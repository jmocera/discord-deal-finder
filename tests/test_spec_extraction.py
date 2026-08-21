"""Tests for extract_clean_specs() and its integration into _process_deals().

Uses only the standard library (unittest + unittest.mock) — this project
deliberately avoids adding new dependencies where the stdlib covers it;
see the project's HANDOFF.md. Runnable via either:
    python -m unittest discover -s tests -p "test_*.py"
    pytest tests/          (if pytest happens to be installed)

Every requests.post call is mocked — these tests never make a real
network call, and never touch the real Discord/Bluesky/Supabase
endpoints deal_bot would otherwise hit.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot import config, pipeline
from deal_bot.ai import spec_extraction


def _mock_response(status_code=200, json_data=None, text=""):
    resp = Mock()
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


def _openrouter_response(content: str):
    """Wraps a message content string in the shape OpenRouter's chat
    completions API actually returns."""
    return _mock_response(200, {"choices": [{"message": {"content": content}}]})


class ExtractCleanSpecsTests(unittest.TestCase):
    def setUp(self):
        # requests.post is mocked in every test below, so the real key's
        # value never matters for whether a network call *succeeds* — but
        # extract_clean_specs() short-circuits before calling requests.post
        # at all if the key looks unset, so tests that need to reach the
        # (mocked) network call need a non-empty key.
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    @patch("deal_bot.ai.client.requests.post")
    def test_valid_response_with_specs(self, mock_post):
        mock_post.return_value = _openrouter_response(json.dumps({
            "clean_title": "Crucial P3 Plus 2TB NVMe SSD",
            "specs": ["Capacity: 2TB", "Interface: PCIe Gen4", "Speed: Up to 5000 MB/s"],
        }))
        result = spec_extraction.extract_clean_specs(
            "Crucial P3 Plus 2TB PCIe Gen4 3D NAND NVMe M.2 SSD, up to 5000MB/s - CT2000P3PSSD8"
        )
        self.assertEqual(result["clean_title"], "Crucial P3 Plus 2TB NVMe SSD")
        self.assertEqual(
            result["specs"],
            ["Capacity: 2TB", "Interface: PCIe Gen4", "Speed: Up to 5000 MB/s"],
        )

    @patch("deal_bot.ai.client.requests.post")
    def test_valid_response_with_zero_specs_is_not_an_error(self, mock_post):
        # A genuinely low-info title should come back with an honest empty
        # list, not be treated as a validation failure (see the plan's
        # anti-hallucination-over-schema-strictness resolution).
        mock_post.return_value = _openrouter_response(json.dumps({
            "clean_title": "6ft HDMI Cable", "specs": [],
        }))
        result = spec_extraction.extract_clean_specs("Generic 6ft HDMI Cable")
        self.assertEqual(result, {"clean_title": "6ft HDMI Cable", "specs": []})

    def test_missing_api_key_skips_network_call_entirely(self):
        config.OPENROUTER_API_KEY = ""
        with patch("deal_bot.ai.client.requests.post") as mock_post:
            result = spec_extraction.extract_clean_specs("Some Raw Messy Title")
            mock_post.assert_not_called()
        self.assertEqual(result, {"clean_title": "Some Raw Messy Title", "specs": []})

    @patch("deal_bot.ai.client.requests.post")
    def test_network_timeout_falls_back(self, mock_post):
        mock_post.side_effect = requests.exceptions.Timeout("timed out")
        result = spec_extraction.extract_clean_specs("Some Raw Messy Title")
        self.assertEqual(result, {"clean_title": "Some Raw Messy Title", "specs": []})

    @patch("deal_bot.ai.client.requests.post")
    def test_http_500_falls_back(self, mock_post):
        mock_post.return_value = _mock_response(500, text="internal server error")
        result = spec_extraction.extract_clean_specs("Some Raw Messy Title")
        self.assertEqual(result, {"clean_title": "Some Raw Messy Title", "specs": []})

    @patch("deal_bot.ai.client.requests.post")
    def test_malformed_json_content_falls_back(self, mock_post):
        mock_post.return_value = _openrouter_response("this is not valid json {{{")
        result = spec_extraction.extract_clean_specs("Some Raw Messy Title")
        self.assertEqual(result, {"clean_title": "Some Raw Messy Title", "specs": []})

    @patch("deal_bot.ai.client.requests.post")
    def test_response_that_isnt_a_json_object_falls_back(self, mock_post):
        # Valid JSON, but a bare list/string instead of the expected object.
        mock_post.return_value = _openrouter_response(json.dumps(["not", "an", "object"]))
        result = spec_extraction.extract_clean_specs("Some Raw Messy Title")
        self.assertEqual(result, {"clean_title": "Some Raw Messy Title", "specs": []})

    @patch("deal_bot.ai.client.requests.post")
    def test_overlong_title_falls_back_to_raw_but_keeps_valid_specs(self, mock_post):
        mock_post.return_value = _openrouter_response(json.dumps({
            "clean_title": "X" * 150,  # over the 100-char limit
            "specs": ["Capacity: 2TB"],
        }))
        result = spec_extraction.extract_clean_specs("Crucial P3 Plus 2TB SSD")
        self.assertEqual(result["clean_title"], "Crucial P3 Plus 2TB SSD")  # fell back to raw title
        self.assertEqual(result["specs"], ["Capacity: 2TB"])  # still kept — it was valid

    @patch("deal_bot.ai.client.requests.post")
    def test_overlong_spec_string_resets_specs_but_keeps_valid_title(self, mock_post):
        mock_post.return_value = _openrouter_response(json.dumps({
            "clean_title": "Crucial P3 Plus 2TB SSD",
            "specs": ["X" * 100],  # over the 60-char limit
        }))
        result = spec_extraction.extract_clean_specs("Crucial P3 Plus 2TB PCIe Gen4 NVMe SSD")
        self.assertEqual(result["clean_title"], "Crucial P3 Plus 2TB SSD")  # unaffected
        self.assertEqual(result["specs"], [])  # reset — it was invalid

    @patch("deal_bot.ai.client.requests.post")
    def test_too_many_specs_resets_the_whole_list(self, mock_post):
        mock_post.return_value = _openrouter_response(json.dumps({
            "clean_title": "Some Product",
            "specs": ["A", "B", "C", "D", "E"],  # 5 > the 4-spec max
        }))
        result = spec_extraction.extract_clean_specs("Some Raw Messy Title")
        self.assertEqual(result["specs"], [])

    @patch("deal_bot.ai.client.requests.post")
    def test_empty_or_blank_title_falls_back_to_raw(self, mock_post):
        mock_post.return_value = _openrouter_response(json.dumps({
            "clean_title": "   ",  # blank after stripping
            "specs": [],
        }))
        result = spec_extraction.extract_clean_specs("Some Raw Messy Title")
        self.assertEqual(result["clean_title"], "Some Raw Messy Title")


class ProcessDealsSteamSkipTests(unittest.TestCase):
    """Verifies the actual _process_deals() branch, not a re-implemented
    copy of its condition — patches out every side-effecting call
    (Discord posting, Supabase pruning) so this never touches real
    services, then asserts extract_clean_specs's call behavior for real
    Steam vs. non-Steam deals run through the genuine pipeline code."""

    def _make_deal(self, source: str) -> dict:
        return {
            "id": f"{source.lower()}:test-123", "source": source,
            "title": "Some Raw Messy Title", "url": "https://example.com/deal",
            "image": None, "sale_price": 20.0, "list_price": 40.0, "discount_pct": 50.0,
        }

    def _run(self, deal: dict):
        stats = {
            "new_count": 0, "skipped_already_seen": 0, "skipped_no_better_price": 0,
            "skipped_below_threshold": 0, "skipped_not_near_historical_low": 0,
            "digest_sent": False,
        }
        digest_stats = {s: {"count": 0, "total_savings": 0.0, "best": None} for s in config.DIGEST_SOURCE_ORDER}
        pipeline._process_deals([deal], seen={}, digest_stats=digest_stats, stats=stats, history_map={})

    @patch("deal_bot.pipeline.build_ai_analysis", return_value="")
    @patch("deal_bot.pipeline.prune_seen")
    @patch("deal_bot.pipeline.post_to_discord", return_value=False)
    @patch("deal_bot.pipeline.extract_clean_specs")
    def test_steam_deal_never_calls_extract_clean_specs(self, mock_extract, mock_post_discord, mock_prune, mock_analysis):
        self._run(self._make_deal("Steam"))
        mock_extract.assert_not_called()

    @patch("deal_bot.pipeline.build_ai_analysis", return_value="")
    @patch("deal_bot.pipeline.prune_seen")
    @patch("deal_bot.pipeline.post_to_discord", return_value=False)
    @patch("deal_bot.pipeline.extract_clean_specs")
    def test_non_steam_deal_calls_extract_clean_specs(self, mock_extract, mock_post_discord, mock_prune, mock_analysis):
        mock_extract.return_value = {"clean_title": "Some Raw Messy Title", "specs": []}
        self._run(self._make_deal("Woot"))
        mock_extract.assert_called_once_with("Some Raw Messy Title")


if __name__ == "__main__":
    unittest.main()