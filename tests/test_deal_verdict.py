"""Tests for the "technical verdict" caption upgrade to build_ai_caption()
and its price-history/spec-context prompt cues.

Stdlib only (unittest + unittest.mock), same convention as
tests/test_spec_extraction.py. Runnable via either:
    python -m unittest discover -s tests -p "test_*.py"
    pytest tests/

Every requests.post / _call_openrouter call is mocked — these tests never
make a real network call or touch the real Discord/Bluesky endpoints.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot.ai import captions
from deal_bot.integrations import bluesky


def _make_deal(**overrides) -> dict:
    deal = {
        "id": "woot:test-123", "source": "Woot", "title": "Raw Messy SEO Title",
        "clean_title": "Clean Product Title", "specs": ["Capacity: 2TB"],
        "url": "https://example.com/deal", "image": None,
        "sale_price": 79.99, "list_price": 159.99, "discount_pct": 50.0,
        "lowest_price": 79.99, "lowest_price_date": None, "is_new_low": False,
    }
    deal.update(overrides)
    return deal


class BuildAiCaptionVerdictTests(unittest.TestCase):
    @patch("deal_bot.ai.captions._call_openrouter")
    def test_new_low_deal_gets_all_time_low_context_in_the_prompt(self, mock_call):
        mock_call.return_value = "Genuine all-time low for this drive. #PCBuild #SSDDeals"
        deal = _make_deal(is_new_low=True)

        result = captions.build_ai_caption(deal)

        # The point of this test: the *prompt actually sent to the model*
        # carries the historical-low signal, not just that some caption
        # came back — this is what makes the caption "data-backed"
        # instead of an ungrounded guess.
        sent_user_prompt = mock_call.call_args[0][2]  # (model, system_prompt, user_prompt, ...)
        self.assertIn("all-time low", sent_user_prompt.lower())
        self.assertTrue(result.startswith("Genuine all-time low"))

    @patch("deal_bot.ai.captions._call_openrouter")
    def test_not_new_low_but_known_floor_price_is_still_passed_as_context(self, mock_call):
        mock_call.return_value = "Solid price, though not its floor. #PCBuild"
        deal = _make_deal(is_new_low=False, sale_price=79.99, lowest_price=59.99)

        captions.build_ai_caption(deal)

        sent_user_prompt = mock_call.call_args[0][2]
        self.assertIn("59.99", sent_user_prompt)

    @patch("deal_bot.ai.captions._call_openrouter")
    def test_specs_are_included_in_the_prompt_when_present(self, mock_call):
        mock_call.return_value = "Fast NVMe storage at a real floor price. #PCBuild"
        deal = _make_deal(specs=["Capacity: 2TB", "Interface: PCIe Gen4"])

        captions.build_ai_caption(deal)

        sent_user_prompt = mock_call.call_args[0][2]
        self.assertIn("Capacity: 2TB", sent_user_prompt)
        self.assertIn("Interface: PCIe Gen4", sent_user_prompt)

    @patch("deal_bot.ai.captions._call_openrouter")
    def test_falls_back_to_template_when_both_models_return_none(self, mock_call):
        mock_call.return_value = None
        deal = _make_deal()

        result = captions.build_ai_caption(deal)

        self.assertEqual(result, captions.build_x_caption(deal))
        self.assertEqual(mock_call.call_count, 2)  # tried primary, then fallback model

    @patch("deal_bot.ai.captions._call_openrouter")
    def test_falls_back_when_response_exceeds_length_ceiling(self, mock_call):
        mock_call.return_value = "X" * 300  # over the 260-char sanity ceiling
        deal = _make_deal()

        result = captions.build_ai_caption(deal)

        self.assertEqual(result, captions.build_x_caption(deal))

    @patch("deal_bot.ai.captions._call_openrouter")
    def test_falls_back_when_hashtags_look_spammy(self, mock_call):
        spammy = "Good deal. " + " ".join(f"#tag{i}" for i in range(10))  # way over 4
        mock_call.return_value = spammy
        deal = _make_deal()

        result = captions.build_ai_caption(deal)

        self.assertEqual(result, captions.build_x_caption(deal))

    @patch("deal_bot.ai.captions._call_openrouter")
    def test_contextual_hashtags_are_kept_not_restricted_to_a_fixed_list(self, mock_call):
        # Deliberate: item-specific hashtags are preserved as-is rather
        # than filtered down to a fixed vocabulary — see the confirmed
        # design decision in this session over the alternative (a hard
        # #gaming/#pcgaming-only allowlist, which was rejected).
        mock_call.return_value = "Real all-time low for this SSD. #SSDDeals #PCBuild #TechDeals"
        deal = _make_deal(is_new_low=True)

        result = captions.build_ai_caption(deal)

        self.assertIn("#SSDDeals", result)
        self.assertIn("#PCBuild", result)
        self.assertIn("#TechDeals", result)


class HashtagSanityCheckTests(unittest.TestCase):
    def test_reasonable_hashtag_count_passes(self):
        self.assertTrue(captions._hashtags_look_reasonable("Good deal. #PCBuild #SSDDeals #TechDeals"))

    def test_no_hashtags_passes(self):
        self.assertTrue(captions._hashtags_look_reasonable("Good deal, no tags here."))

    def test_too_many_hashtags_fails(self):
        text = "Deal. " + " ".join(f"#tag{i}" for i in range(6))
        self.assertFalse(captions._hashtags_look_reasonable(text))


class BlueskyLengthLimitTests(unittest.TestCase):
    """Confirms the 300-grapheme budget holds end-to-end for the new,
    potentially longer/differently-shaped verdict-style captions, and that
    the URL line is never lost or clipped."""

    @patch("deal_bot.integrations.bluesky.requests.post")
    @patch("deal_bot.integrations.bluesky._build_bluesky_embed", return_value=None)
    @patch("deal_bot.integrations.bluesky._bluesky_login")
    @patch("deal_bot.integrations.bluesky.build_ai_caption_body")
    def test_post_text_never_exceeds_300_chars_even_with_an_oversized_caption(
        self, mock_caption, mock_login, mock_embed, mock_post
    ):
        mock_login.return_value = {"accessJwt": "test-jwt", "did": "did:plc:test"}
        # Deliberately oversized, as if a verdict caption ran long — the
        # body is patched; fit_deal_post appends and fits the URL itself.
        mock_caption.return_value = "A" * 290
        mock_post.return_value = Mock(status_code=200)

        deal = _make_deal(url="https://example.com/deal")
        ok = bluesky.post_to_bluesky(deal)

        self.assertTrue(ok)
        sent_record = mock_post.call_args.kwargs["json"]["record"]
        self.assertLessEqual(len(sent_record["text"]), 300)
        self.assertTrue(sent_record["text"].endswith(deal["url"]))


class HashtagPreservationTests(unittest.TestCase):
    """The bug: posts ended with "…" because the old safety net tail-sliced
    the caption body, chopping the trailing hashtags. fit_deal_post must
    trim the prose first and keep the hashtag run + URL intact."""

    @patch("deal_bot.integrations.bluesky.requests.post")
    @patch("deal_bot.integrations.bluesky._build_bluesky_embed", return_value=None)
    @patch("deal_bot.integrations.bluesky._bluesky_login")
    @patch("deal_bot.integrations.bluesky.build_ai_caption_body")
    def test_hashtags_survive_truncation(self, mock_caption, mock_login, mock_embed, mock_post):
        mock_login.return_value = {"accessJwt": "test-jwt", "did": "did:plc:test"}
        mock_caption.return_value = "A" * 290 + " #PCBuild #SSDDeals"
        mock_post.return_value = Mock(status_code=200)

        deal = _make_deal(url="https://example.com/deal")
        ok = bluesky.post_to_bluesky(deal)

        self.assertTrue(ok)
        sent_record = mock_post.call_args.kwargs["json"]["record"]
        self.assertLessEqual(len(sent_record["text"]), 300)
        self.assertTrue(sent_record["text"].endswith(deal["url"]))
        self.assertIn("#PCBuild #SSDDeals", sent_record["text"])

    @patch("deal_bot.integrations.bluesky.requests.post")
    @patch("deal_bot.integrations.bluesky._build_bluesky_embed", return_value=None)
    @patch("deal_bot.integrations.bluesky._bluesky_login")
    @patch("deal_bot.integrations.bluesky.build_ai_caption_body")
    def test_short_caption_posts_without_ellipsis(self, mock_caption, mock_login, mock_embed, mock_post):
        mock_login.return_value = {"accessJwt": "test-jwt", "did": "did:plc:test"}
        mock_caption.return_value = "Genuine floor price for this drive. #SSDDeals"
        mock_post.return_value = Mock(status_code=200)

        deal = _make_deal(url="https://example.com/deal")
        ok = bluesky.post_to_bluesky(deal)

        self.assertTrue(ok)
        sent_record = mock_post.call_args.kwargs["json"]["record"]
        self.assertEqual(sent_record["text"], mock_caption.return_value + "\n" + deal["url"])
        self.assertNotIn("…", sent_record["text"])

    def test_embed_failure_never_breaks_the_text_post(self):
        # _build_bluesky_embed returning None must only drop the card — the
        # text + facets still go out. (Direct fit check, no network.)
        deal = _make_deal()
        body = "Solid deal for builders. #PCBuild"
        out = bluesky.fit_deal_post(body, deal["url"])
        self.assertIn("#PCBuild", out)
        self.assertTrue(out.endswith(deal["url"]))


class BuildAiCaptionBodyBudgetTests(unittest.TestCase):
    @patch("deal_bot.ai.captions._call_openrouter")
    def test_exactly_budget_caption_is_accepted(self, mock_call):
        url = "https://example.com/deal"
        budget = captions.caption_budget(url)
        mock_call.return_value = "D" * budget
        deal = _make_deal(url=url)

        result = captions.build_ai_caption_body(deal)

        self.assertEqual(result, "D" * budget)

    @patch("deal_bot.ai.captions._call_openrouter")
    def test_over_budget_caption_falls_through_to_next_model(self, mock_call):
        url = "https://example.com/deal"
        budget = captions.caption_budget(url)
        mock_call.side_effect = ["E" * (budget + 1), "E" * (budget + 1)]
        deal = _make_deal(url=url)

        result = captions.build_ai_caption_body(deal)

        self.assertEqual(result, captions.build_x_caption_body(deal))
        self.assertEqual(mock_call.call_count, 2)

    @patch("deal_bot.ai.captions._call_openrouter")
    def test_caption_with_a_url_is_rejected(self, mock_call):
        mock_call.side_effect = ["Good deal. https://evil.example.com #PCBuild", "Good deal. #PCBuild"]
        deal = _make_deal()

        result = captions.build_ai_caption_body(deal)

        self.assertEqual(result, "Good deal. #PCBuild")  # first model's URL was rejected


if __name__ == "__main__":
    unittest.main()