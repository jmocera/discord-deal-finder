"""Tests for the enhanced deal analysis (ai/deal_analyst.py).

Stdlib only (unittest + unittest.mock). Every _call_openrouter call is
mocked — no real network calls.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot.ai import deal_analyst


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


class BuildAiAnalysisTests(unittest.TestCase):
    @patch("deal_bot.ai.deal_analyst._call_openrouter")
    def test_returns_analysis_on_valid_response(self, mock_call):
        mock_call.return_value = "A strong boot drive at a real floor price. #ignored"
        result = deal_analyst.build_ai_analysis(_make_deal())
        self.assertEqual(result, "A strong boot drive at a real floor price. #ignored")

    @patch("deal_bot.ai.deal_analyst._call_openrouter")
    def test_fails_open_to_empty_string_when_both_models_fail(self, mock_call):
        mock_call.return_value = None
        result = deal_analyst.build_ai_analysis(_make_deal())
        self.assertEqual(result, "")
        self.assertEqual(mock_call.call_count, 2)

    @patch("deal_bot.ai.deal_analyst._call_openrouter")
    def test_fails_open_when_over_length_ceiling(self, mock_call):
        mock_call.return_value = "X" * 500  # over the 380-char ceiling
        result = deal_analyst.build_ai_analysis(_make_deal())
        self.assertEqual(result, "")

    @patch("deal_bot.ai.deal_analyst._call_openrouter")
    def test_prompt_carries_specs_and_price_history_context(self, mock_call):
        mock_call.return_value = "Good value."
        deal = _make_deal(is_new_low=True, specs=["Capacity: 2TB", "Interface: PCIe Gen4"])

        deal_analyst.build_ai_analysis(deal)

        sent_user_prompt = mock_call.call_args[0][2]
        self.assertIn("Capacity: 2TB", sent_user_prompt)
        self.assertIn("Interface: PCIe Gen4", sent_user_prompt)
        self.assertIn("all-time low", sent_user_prompt.lower())


if __name__ == "__main__":
    unittest.main()