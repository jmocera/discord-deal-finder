"""Tests for vet_amazon_deal.py — ASIN extraction/canonicalization, field
validation, deterministic risk assessment, and #ad-disclosure copy
formatting.

Standard library only (unittest + unittest.mock), consistent with the
rest of this project's test suite. Runnable via either:
    python -m unittest discover -s tests -p "test_*.py"
    pytest tests/

Every requests call is mocked — these tests never make a real network
call, and never touch the real Amazon/OpenRouter endpoints
vet_amazon_deal.py would otherwise hit.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import vet_amazon_deal as vet
from deal_bot import config


def _mock_response(status_code=200, json_data=None, text="", url=None):
    resp = Mock()
    resp.status_code = status_code
    resp.text = text
    if url is not None:
        resp.url = url
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


def _openrouter_response(content: str):
    return _mock_response(200, {"choices": [{"message": {"content": content}}]})


# ---------------------------------------------------------------------------
# ASIN extraction & URL canonicalization
# ---------------------------------------------------------------------------
class AsinExtractionTests(unittest.TestCase):
    def test_dp_path(self):
        self.assertEqual(vet._asin_from_url_text("https://www.amazon.com/dp/B08N5WRWNW"), "B08N5WRWNW")

    def test_dp_path_with_product_slug_and_ref(self):
        url = "https://www.amazon.com/Some-Product-Name/dp/B08N5WRWNW/ref=sr_1_3?keywords=ssd&qid=123&sr=8-3"
        self.assertEqual(vet._asin_from_url_text(url), "B08N5WRWNW")

    def test_gp_product_path(self):
        self.assertEqual(vet._asin_from_url_text("https://www.amazon.com/gp/product/B08N5WRWNW"), "B08N5WRWNW")

    def test_asin_query_param(self):
        self.assertEqual(vet._asin_from_url_text("https://www.amazon.com/exec/obidos/redirect?asin=B08N5WRWNW"), "B08N5WRWNW")

    def test_lowercase_asin_is_uppercased(self):
        self.assertEqual(vet._asin_from_url_text("https://www.amazon.com/dp/b08n5wrwnw"), "B08N5WRWNW")

    def test_no_asin_found_returns_none(self):
        self.assertIsNone(vet._asin_from_url_text("https://www.amazon.com/s?k=ssd"))

    def test_extract_asin_skips_network_when_regex_matches(self):
        with patch("vet_amazon_deal.requests.get") as mock_get:
            asin = vet.extract_asin("https://www.amazon.com/dp/B08N5WRWNW")
            mock_get.assert_not_called()
        self.assertEqual(asin, "B08N5WRWNW")

    @patch("vet_amazon_deal.requests.get")
    def test_extract_asin_resolves_shortened_url(self, mock_get):
        mock_get.return_value = _mock_response(200, url="https://www.amazon.com/dp/B08N5WRWNW/ref=abc")
        asin = vet.extract_asin("https://a.co/d/xyz123")
        self.assertEqual(asin, "B08N5WRWNW")

    @patch("vet_amazon_deal.requests.get")
    def test_extract_asin_network_failure_falls_back_to_none(self, mock_get):
        mock_get.side_effect = vet.requests.exceptions.Timeout("timed out")
        self.assertIsNone(vet.extract_asin("https://a.co/d/xyz123"))


class CanonicalUrlTests(unittest.TestCase):
    def test_canonical_url_shape(self):
        self.assertEqual(
            vet.canonical_amazon_url("B08N5WRWNW"),
            "https://www.amazon.com/dp/B08N5WRWNW?tag=voltdrop05-20",
        )

    def test_canonical_url_drops_all_original_tracking_params(self):
        # Rebuilt from ASIN alone, so ref=/qid=/sr=/keywords=/etc. from the
        # original URL are never even looked at, let alone carried over.
        url = vet.canonical_amazon_url("B08N5WRWNW")
        for junk in ("ref=", "qid=", "sr=", "keywords="):
            self.assertNotIn(junk, url)


class LooksLikeUrlTests(unittest.TestCase):
    def test_http_and_https_recognized(self):
        self.assertTrue(vet.looks_like_url("https://www.amazon.com/dp/B08N5WRWNW"))
        self.assertTrue(vet.looks_like_url("http://amzn.to/abc"))

    def test_plain_text_not_recognized(self):
        self.assertFalse(vet.looks_like_url("Crucial P3 Plus 2TB SSD $79.99"))


# ---------------------------------------------------------------------------
# _parse_vetting_json — strict per-field validation, no coercion
# ---------------------------------------------------------------------------
class ParseVettingJsonTests(unittest.TestCase):
    def test_fully_valid_response(self):
        fields = vet._parse_vetting_json(json.dumps({
            "clean_title": "Crucial P3 Plus 2TB NVMe SSD",
            "sale_price": 79.99,
            "list_or_typical_price": 159.99,
            "seller_type": "Sold/Shipped by Amazon",
            "review_count": 4200,
            "rating": 4.6,
        }))
        self.assertEqual(fields["clean_title"], "Crucial P3 Plus 2TB NVMe SSD")
        self.assertEqual(fields["sale_price"], 79.99)
        self.assertEqual(fields["list_or_typical_price"], 159.99)
        self.assertEqual(fields["seller_type"], "Sold/Shipped by Amazon")
        self.assertEqual(fields["review_count"], 4200)
        self.assertEqual(fields["rating"], 4.6)

    def test_all_nulls_is_valid_not_an_error(self):
        fields = vet._parse_vetting_json(json.dumps({
            "clean_title": None, "sale_price": None, "list_or_typical_price": None,
            "seller_type": None, "review_count": None, "rating": None,
        }))
        self.assertEqual(fields, vet._EMPTY_FIELDS)

    def test_none_content_falls_back(self):
        self.assertEqual(vet._parse_vetting_json(None), vet._EMPTY_FIELDS)

    def test_malformed_json_falls_back(self):
        self.assertEqual(vet._parse_vetting_json("not valid json {{{"), vet._EMPTY_FIELDS)

    def test_non_object_json_falls_back(self):
        self.assertEqual(vet._parse_vetting_json(json.dumps(["a", "b"])), vet._EMPTY_FIELDS)

    def test_invalid_seller_type_falls_back_to_none_but_keeps_rest(self):
        fields = vet._parse_vetting_json(json.dumps({
            "clean_title": "Some Product", "sale_price": 10.0, "list_or_typical_price": None,
            "seller_type": "Definitely Legit Seller", "review_count": 500, "rating": 4.5,
        }))
        self.assertIsNone(fields["seller_type"])
        self.assertEqual(fields["clean_title"], "Some Product")
        self.assertEqual(fields["review_count"], 500)

    def test_rating_out_of_range_falls_back_to_none(self):
        fields = vet._parse_vetting_json(json.dumps({
            "clean_title": None, "sale_price": None, "list_or_typical_price": None,
            "seller_type": None, "review_count": None, "rating": 5.5,
        }))
        self.assertIsNone(fields["rating"])

    def test_negative_review_count_falls_back_to_none(self):
        fields = vet._parse_vetting_json(json.dumps({
            "clean_title": None, "sale_price": None, "list_or_typical_price": None,
            "seller_type": None, "review_count": -5, "rating": None,
        }))
        self.assertIsNone(fields["review_count"])

    def test_zero_or_negative_price_falls_back_to_none(self):
        fields = vet._parse_vetting_json(json.dumps({
            "clean_title": None, "sale_price": 0, "list_or_typical_price": -10,
            "seller_type": None, "review_count": None, "rating": None,
        }))
        self.assertIsNone(fields["sale_price"])
        self.assertIsNone(fields["list_or_typical_price"])

    def test_string_price_is_rejected_not_coerced(self):
        # No coercion, by design — a string where a number is expected
        # falls back to None rather than being parsed.
        fields = vet._parse_vetting_json(json.dumps({
            "clean_title": None, "sale_price": "79.99", "list_or_typical_price": None,
            "seller_type": None, "review_count": None, "rating": None,
        }))
        self.assertIsNone(fields["sale_price"])

    def test_bool_is_rejected_for_numeric_fields(self):
        # bool is a subclass of int in Python — must not slip through the
        # isinstance(..., (int, float)) checks.
        fields = vet._parse_vetting_json(json.dumps({
            "clean_title": None, "sale_price": None, "list_or_typical_price": None,
            "seller_type": None, "review_count": True, "rating": None,
        }))
        self.assertIsNone(fields["review_count"])

    def test_overlong_title_falls_back_to_none(self):
        fields = vet._parse_vetting_json(json.dumps({
            "clean_title": "X" * 200, "sale_price": None, "list_or_typical_price": None,
            "seller_type": None, "review_count": None, "rating": None,
        }))
        self.assertIsNone(fields["clean_title"])


# ---------------------------------------------------------------------------
# compute_risk_assessment — deterministic Python, not model judgment
# ---------------------------------------------------------------------------
class RiskAssessmentTests(unittest.TestCase):
    def _good_fields(self, **overrides):
        fields = {
            "clean_title": "Crucial P3 Plus 2TB NVMe SSD", "sale_price": 79.99,
            "list_or_typical_price": 159.99, "seller_type": "Sold/Shipped by Amazon",
            "review_count": 4200, "rating": 4.6,
        }
        fields.update(overrides)
        return fields

    def test_all_clear_passes_with_no_warnings(self):
        risk = vet.compute_risk_assessment(self._good_fields())
        self.assertTrue(risk["passed"])
        self.assertEqual(risk["warnings"], [])
        self.assertTrue(risk["verdict"].startswith("PASS"))

    def test_third_party_seller_flagged(self):
        risk = vet.compute_risk_assessment(self._good_fields(seller_type="3rd-Party Direct"))
        self.assertFalse(risk["passed"])
        self.assertTrue(any("3rd-Party Direct" in w for w in risk["warnings"]))

    def test_unknown_seller_type_flagged(self):
        risk = vet.compute_risk_assessment(self._good_fields(seller_type=None))
        self.assertFalse(risk["passed"])
        self.assertTrue(any("could not be determined" in w for w in risk["warnings"]))

    def test_low_review_count_flagged(self):
        risk = vet.compute_risk_assessment(self._good_fields(review_count=20))
        self.assertFalse(risk["passed"])
        self.assertTrue(any("Low review count" in w for w in risk["warnings"]))

    def test_review_count_at_threshold_not_flagged(self):
        risk = vet.compute_risk_assessment(self._good_fields(review_count=100))
        self.assertTrue(risk["passed"])

    def test_low_rating_flagged(self):
        risk = vet.compute_risk_assessment(self._good_fields(rating=3.2))
        self.assertFalse(risk["passed"])
        self.assertTrue(any("Low rating" in w for w in risk["warnings"]))

    def test_rating_at_threshold_not_flagged(self):
        risk = vet.compute_risk_assessment(self._good_fields(rating=4.0))
        self.assertTrue(risk["passed"])

    def test_no_real_discount_flagged(self):
        risk = vet.compute_risk_assessment(self._good_fields(sale_price=159.99, list_or_typical_price=159.99))
        self.assertFalse(risk["passed"])
        self.assertTrue(any("not actually below" in w for w in risk["warnings"]))

    def test_sale_price_above_reference_flagged(self):
        risk = vet.compute_risk_assessment(self._good_fields(sale_price=200.0, list_or_typical_price=159.99))
        self.assertFalse(risk["passed"])

    def test_missing_price_data_does_not_trigger_discount_warning(self):
        # Can't judge "is this a real discount" without both numbers —
        # absence of data isn't treated as a fabricated failure.
        risk = vet.compute_risk_assessment(self._good_fields(sale_price=None, list_or_typical_price=None))
        self.assertTrue(risk["passed"])

    def test_multiple_warnings_all_collected(self):
        risk = vet.compute_risk_assessment(self._good_fields(
            seller_type="3rd-Party Direct", review_count=5, rating=2.0,
        ))
        self.assertEqual(len(risk["warnings"]), 3)


class DiscountPctTests(unittest.TestCase):
    def test_normal_discount(self):
        self.assertEqual(vet._discount_pct({"sale_price": 80.0, "list_or_typical_price": 160.0}), 50.0)

    def test_missing_prices_returns_none(self):
        self.assertIsNone(vet._discount_pct({"sale_price": None, "list_or_typical_price": 160.0}))
        self.assertIsNone(vet._discount_pct({"sale_price": 80.0, "list_or_typical_price": None}))

    def test_no_real_discount_returns_none(self):
        self.assertIsNone(vet._discount_pct({"sale_price": 160.0, "list_or_typical_price": 160.0}))
        self.assertIsNone(vet._discount_pct({"sale_price": 200.0, "list_or_typical_price": 160.0}))


# ---------------------------------------------------------------------------
# Ready-to-copy formatting — #ad must always lead
# ---------------------------------------------------------------------------
class CopyFormattingTests(unittest.TestCase):
    def _vetted(self, **overrides):
        vetted = {
            "clean_title": "Crucial P3 Plus 2TB NVMe SSD", "sale_price": 79.99,
            "list_or_typical_price": 159.99, "seller_type": "Sold/Shipped by Amazon",
            "review_count": 4200, "rating": 4.6, "asin": "B08N5WRWNW",
            "canonical_url": "https://www.amazon.com/dp/B08N5WRWNW?tag=voltdrop05-20",
        }
        vetted.update(overrides)
        return vetted

    def test_discord_copy_starts_with_ad(self):
        self.assertTrue(vet.format_discord_copy(self._vetted()).startswith("#ad "))

    def test_bluesky_copy_starts_with_ad(self):
        self.assertTrue(vet.format_bluesky_copy(self._vetted()).startswith("#ad "))

    def test_bluesky_copy_starts_with_ad_even_with_missing_fields(self):
        vetted = self._vetted(clean_title=None, sale_price=None, list_or_typical_price=None, canonical_url=None)
        self.assertTrue(vet.format_bluesky_copy(vetted).startswith("#ad "))

    def test_discord_copy_starts_with_ad_even_with_missing_fields(self):
        vetted = self._vetted(clean_title=None, sale_price=None, list_or_typical_price=None, canonical_url=None)
        self.assertTrue(vet.format_discord_copy(vetted).startswith("#ad "))

    def test_discord_copy_includes_canonical_url(self):
        text = vet.format_discord_copy(self._vetted())
        self.assertIn("https://www.amazon.com/dp/B08N5WRWNW?tag=voltdrop05-20", text)

    def test_bluesky_copy_respects_300_char_limit(self):
        vetted = self._vetted(clean_title="X" * 400)
        text = vet.format_bluesky_copy(vetted)
        self.assertLessEqual(len(text), 300)

    def test_bluesky_copy_truncation_still_starts_with_ad_and_keeps_url(self):
        vetted = self._vetted(clean_title="X" * 400)
        text = vet.format_bluesky_copy(vetted)
        self.assertTrue(text.startswith("#ad "))
        self.assertIn("https://www.amazon.com/dp/B08N5WRWNW?tag=voltdrop05-20", text)

    def test_bluesky_copy_url_is_the_last_line_and_under_hard_target(self):
        vetted = self._vetted(clean_title="X" * 400)
        text = vet.format_bluesky_copy(vetted)
        self.assertLessEqual(len(text), 298)
        self.assertTrue(text.endswith("https://www.amazon.com/dp/B08N5WRWNW?tag=voltdrop05-20"))


# ---------------------------------------------------------------------------
# _call_openrouter — missing key, network errors, non-200, empty content,
# code-fence stripping
# ---------------------------------------------------------------------------
class OpenRouterCallTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    def test_missing_api_key_skips_network_call(self):
        config.OPENROUTER_API_KEY = ""
        with patch("deal_bot.transport.request") as mock_post:
            result = vet.call_openrouter("some-model", "system", "user")
            mock_post.assert_not_called()
        self.assertIsNone(result)

    @patch("deal_bot.transport.request")
    def test_network_failure_returns_none(self, mock_request):
        # transport.request returns None only after exhausting network retries
        # — the client's fail-open path for a hard network failure.
        mock_request.return_value = None
        self.assertIsNone(vet.call_openrouter("some-model", "system", "user"))

    @patch("deal_bot.transport.request")
    def test_http_500_returns_none(self, mock_post):
        mock_post.return_value = _mock_response(500, text="internal server error")
        self.assertIsNone(vet.call_openrouter("some-model", "system", "user"))

    @patch("deal_bot.transport.request")
    def test_empty_content_returns_none(self, mock_post):
        mock_post.return_value = _openrouter_response("")
        self.assertIsNone(vet.call_openrouter("some-model", "system", "user"))

    @patch("deal_bot.transport.request")
    def test_code_fence_wrapped_json_is_stripped(self, mock_post):
        mock_post.return_value = _openrouter_response('```json\n{"a": 1}\n```')
        result = vet.call_openrouter("some-model", "system", "user")
        self.assertEqual(result, '{"a": 1}')

    @patch("deal_bot.transport.request")
    def test_list_user_content_accepted_for_vision(self, mock_post):
        mock_post.return_value = _openrouter_response('{"ok": true}')
        content_blocks = [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]
        result = vet.call_openrouter("vision-model", "system", content_blocks)
        self.assertEqual(result, '{"ok": true}')
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["messages"][1]["content"], content_blocks)

    def test_public_alias_is_the_private_implementation(self):
        # vet_amazon_deal.py imports the public alias; it must be the same
        # function object the package internals and tests patch.
        from deal_bot.ai import client as or_client
        self.assertIs(vet.call_openrouter, or_client._call_openrouter)


# ---------------------------------------------------------------------------
# vet_from_text / vet_from_image — full pipeline, network mocked
# ---------------------------------------------------------------------------
class VetFromTextTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    @patch("deal_bot.transport.request")
    def test_full_pipeline_with_url_produces_canonical_link_and_risk(self, mock_post):
        mock_post.return_value = _openrouter_response(json.dumps({
            "clean_title": "Crucial P3 Plus 2TB NVMe SSD", "sale_price": 79.99,
            "list_or_typical_price": 159.99, "seller_type": "Sold/Shipped by Amazon",
            "review_count": 4200, "rating": 4.6,
        }))
        result = vet.vet_from_text("pasted page text", source_url="https://www.amazon.com/dp/B08N5WRWNW")
        self.assertEqual(result["asin"], "B08N5WRWNW")
        self.assertEqual(result["canonical_url"], "https://www.amazon.com/dp/B08N5WRWNW?tag=voltdrop05-20")
        self.assertTrue(result["risk_assessment"]["passed"])

    @patch("deal_bot.transport.request")
    def test_no_source_url_means_no_canonical_link(self, mock_post):
        mock_post.return_value = _openrouter_response(json.dumps({
            "clean_title": "Some Product", "sale_price": 10.0, "list_or_typical_price": None,
            "seller_type": None, "review_count": None, "rating": None,
        }))
        result = vet.vet_from_text("pasted page text", source_url=None)
        self.assertIsNone(result["asin"])
        self.assertIsNone(result["canonical_url"])

    @patch("deal_bot.transport.request")
    def test_openrouter_failure_still_returns_a_full_shape_with_risk_assessment(self, mock_request):
        # transport.request returns None only after exhausting network retries
        # — the client's fail-open path, which must still yield a full vetted
        # shape with a risk assessment rather than raising.
        mock_request.return_value = None
        result = vet.vet_from_text("pasted page text", source_url="https://www.amazon.com/dp/B08N5WRWNW")
        self.assertIsNone(result["clean_title"])
        self.assertIn("risk_assessment", result)
        self.assertFalse(result["risk_assessment"]["passed"])  # seller type unknown


class VetFromImageTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    @patch("deal_bot.transport.request")
    def test_full_pipeline_reads_and_encodes_real_file(self, mock_post):
        mock_post.return_value = _openrouter_response(json.dumps({
            "clean_title": "Crucial P3 Plus 2TB NVMe SSD", "sale_price": 79.99,
            "list_or_typical_price": 159.99, "seller_type": "Sold/Shipped by Amazon",
            "review_count": 4200, "rating": 4.6,
        }))
        with patch("pathlib.Path.read_bytes", return_value=b"\x89PNG\r\n\x1a\nfakepngdata"), \
             patch("pathlib.Path.is_file", return_value=True):
            result = vet.vet_from_image("screenshot.png")
        self.assertEqual(result["clean_title"], "Crucial P3 Plus 2TB NVMe SSD")
        self.assertTrue(result["risk_assessment"]["passed"])
        # Confirms the image content actually reached the vision call.
        sent_content = mock_post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIsInstance(sent_content, list)
        self.assertTrue(any(block.get("type") == "image_url" for block in sent_content))

    def test_missing_file_falls_back_without_calling_openrouter(self):
        with patch("deal_bot.transport.request") as mock_post:
            result = vet.vet_from_image("does/not/exist.png")
            mock_post.assert_not_called()
        self.assertEqual(result["clean_title"], vet._EMPTY_FIELDS["clean_title"])

    def test_oversized_image_falls_back_without_calling_openrouter(self):
        with patch("pathlib.Path.read_bytes", return_value=b"0" * (vet._MAX_IMAGE_BYTES + 1)), \
             patch("deal_bot.transport.request") as mock_post:
            result = vet.vet_from_image("huge.png")
            mock_post.assert_not_called()
        self.assertIsNone(result["clean_title"])


# ---------------------------------------------------------------------------
# fetch_amazon_page_text — best-effort fetch, no bot-detection evasion
# ---------------------------------------------------------------------------
class FetchAmazonPageTextTests(unittest.TestCase):
    @patch("vet_amazon_deal.requests.get")
    def test_successful_fetch_strips_html(self, mock_get):
        mock_get.return_value = _mock_response(
            200, text="<html><body><script>var x=1;</script><h1>Title</h1><p>" + ("Real product description text. " * 20) + "</p></body></html>",
        )
        result = vet.fetch_amazon_page_text("https://www.amazon.com/dp/B08N5WRWNW")
        self.assertIsNotNone(result)
        self.assertNotIn("<script>", result)
        self.assertNotIn("var x=1", result)
        self.assertIn("Title", result)

    @patch("vet_amazon_deal.requests.get")
    def test_non_200_returns_none(self, mock_get):
        mock_get.return_value = _mock_response(503, text="Service Unavailable")
        self.assertIsNone(vet.fetch_amazon_page_text("https://www.amazon.com/dp/B08N5WRWNW"))

    @patch("vet_amazon_deal.requests.get")
    def test_network_error_returns_none(self, mock_get):
        mock_get.side_effect = vet.requests.exceptions.ConnectionError("refused")
        self.assertIsNone(vet.fetch_amazon_page_text("https://www.amazon.com/dp/B08N5WRWNW"))

    @patch("vet_amazon_deal.requests.get")
    def test_sparse_page_treated_as_likely_captcha_block(self, mock_get):
        mock_get.return_value = _mock_response(200, text="<html><body>Robot Check</body></html>")
        self.assertIsNone(vet.fetch_amazon_page_text("https://www.amazon.com/dp/B08N5WRWNW"))


# ---------------------------------------------------------------------------
# run_url_mode routing — URL vs. raw text dispatch
# ---------------------------------------------------------------------------
class RunUrlModeRoutingTests(unittest.TestCase):
    @patch("vet_amazon_deal.run_text_mode")
    def test_non_url_input_routes_to_text_mode(self, mock_run_text):
        vet.run_url_mode("Crucial P3 Plus 2TB SSD, $79.99, 4.6 stars, 4200 ratings")
        mock_run_text.assert_called_once()
        args, kwargs = mock_run_text.call_args
        self.assertIn("Crucial P3 Plus", args[0])

    @patch("vet_amazon_deal.print_report")
    @patch("vet_amazon_deal.vet_from_text")
    @patch("vet_amazon_deal.fetch_amazon_page_text", return_value="enough page text " * 20)
    def test_url_input_fetches_and_vets(self, mock_fetch, mock_vet, mock_print):
        mock_vet.return_value = {"risk_assessment": {"passed": True, "warnings": [], "verdict": "PASS"}}
        vet.run_url_mode("https://www.amazon.com/dp/B08N5WRWNW")
        mock_fetch.assert_called_once_with("https://www.amazon.com/dp/B08N5WRWNW")
        mock_vet.assert_called_once()

    @patch("vet_amazon_deal.fetch_amazon_page_text", return_value=None)
    def test_failed_fetch_does_not_call_openrouter(self, mock_fetch):
        with patch("deal_bot.transport.request") as mock_post:
            vet.run_url_mode("https://www.amazon.com/dp/B08N5WRWNW")
            mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
