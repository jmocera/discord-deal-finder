"""Tests for the shadow-mode category tagger (ai/categorizer.py).

Stdlib only (unittest + unittest.mock). Every _call_openrouter call is
mocked — no real network calls.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot import config
from deal_bot.ai import categorizer


def _make_deal(i: int) -> dict:
    return {
        "id": f"woot:test-{i}", "source": "Woot", "title": f"Deal {i}",
        "url": "https://example.com/deal", "sale_price": 10.0 * i,
        "list_price": 20.0 * i, "discount_pct": 50.0,
    }


class CategorizeDealsTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    def test_missing_api_key_skips_network_call(self):
        config.OPENROUTER_API_KEY = ""
        with patch("deal_bot.ai.categorizer._call_openrouter") as mock_call:
            categories, model = categorizer.categorize_deals([_make_deal(1)])
            mock_call.assert_not_called()
        self.assertEqual(categories, {})
        self.assertIsNone(model)

    def test_empty_deals_returns_empty(self):
        self.assertEqual(categorizer.categorize_deals([]), ({}, None))

    @patch("deal_bot.ai.categorizer._call_openrouter")
    def test_valid_categories_are_parsed(self, mock_call):
        mock_call.return_value = "storage\ncomponent\ngame"
        deals = [_make_deal(i) for i in (1, 2, 3)]

        categories, model = categorizer.categorize_deals(deals)

        self.assertEqual(model, config.OPENROUTER_CATEGORIZER_MODEL)
        self.assertEqual(categories, {"woot:test-1": "storage", "woot:test-2": "component", "woot:test-3": "game"})

    @patch("deal_bot.ai.categorizer._call_openrouter")
    def test_fails_open_when_both_models_return_none(self, mock_call):
        mock_call.return_value = None
        categories, model = categorizer.categorize_deals([_make_deal(1)])
        self.assertEqual(categories, {})
        self.assertIsNone(model)
        self.assertEqual(mock_call.call_count, 2)

    @patch("deal_bot.ai.categorizer._call_openrouter")
    def test_wrong_line_count_fails_open(self, mock_call):
        mock_call.return_value = "storage\ncomponent"  # 2 lines for 3 deals
        categories, model = categorizer.categorize_deals([_make_deal(i) for i in (1, 2, 3)])
        self.assertEqual(categories, {})
        self.assertIsNone(model)

    @patch("deal_bot.ai.categorizer._call_openrouter")
    def test_invalid_category_fails_open(self, mock_call):
        mock_call.return_value = "storage\ntoaster"  # not a known category
        categories, model = categorizer.categorize_deals([_make_deal(i) for i in (1, 2)])
        self.assertEqual(categories, {})
        self.assertIsNone(model)

    @patch("deal_bot.ai.categorizer._call_openrouter")
    def test_prompt_carries_the_deals(self, mock_call):
        mock_call.return_value = "storage\ncomponent"
        deals = [_make_deal(1), _make_deal(2)]

        categorizer.categorize_deals(deals)

        sent_user_prompt = mock_call.call_args[0][2]
        self.assertIn("Deal 1", sent_user_prompt)
        self.assertIn("Deal 2", sent_user_prompt)

    @patch("deal_bot.ai.categorizer._call_openrouter")
    def test_reasoning_is_omitted_for_gemma(self, mock_call):
        mock_call.return_value = "storage"
        categorizer.categorize_deals([_make_deal(1)])
        self.assertNotIn("reasoning", mock_call.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()