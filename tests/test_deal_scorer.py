"""Tests for the shadow-mode deal quality scorer (ai/deal_scorer.py).

Stdlib only (unittest + unittest.mock), consistent with the rest of the
suite. Every _call_openrouter call is mocked — no real network calls.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot import config
from deal_bot.ai import deal_scorer


def _make_deal(i: int) -> dict:
    return {
        "id": f"woot:test-{i}", "source": "Woot", "title": f"Deal {i}",
        "url": "https://example.com/deal", "sale_price": 10.0 * i,
        "list_price": 20.0 * i, "discount_pct": 50.0,
    }


class ScoreDealsTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    def test_missing_api_key_skips_network_call(self):
        config.OPENROUTER_API_KEY = ""
        with patch("deal_bot.ai.deal_scorer._call_openrouter") as mock_call:
            scores, model = deal_scorer.score_deals([_make_deal(1)])
            mock_call.assert_not_called()
        self.assertEqual(scores, {})
        self.assertIsNone(model)

    def test_empty_deals_returns_empty(self):
        self.assertEqual(deal_scorer.score_deals([]), ({}, None))

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_valid_scores_are_parsed(self, mock_call):
        mock_call.return_value = "9\n10\n1"
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(model, config.OPENROUTER_QUALITY_SCORER_MODEL)
        self.assertEqual(scores, {"woot:test-1": 9, "woot:test-2": 10, "woot:test-3": 1})

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_fails_open_when_both_models_return_none(self, mock_call):
        mock_call.return_value = None
        scores, model = deal_scorer.score_deals([_make_deal(1)])
        self.assertEqual(scores, {})
        self.assertIsNone(model)
        self.assertEqual(mock_call.call_count, 2)  # primary + fallback

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_wrong_line_count_fails_open(self, mock_call):
        mock_call.return_value = "9\n10"  # 2 lines for 3 deals
        scores, model = deal_scorer.score_deals([_make_deal(i) for i in (1, 2, 3)])
        self.assertEqual(scores, {})
        self.assertIsNone(model)

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_out_of_range_score_fails_open(self, mock_call):
        mock_call.return_value = "9\n42\n1"  # 42 not in 1..10
        scores, model = deal_scorer.score_deals([_make_deal(i) for i in (1, 2, 3)])
        self.assertEqual(scores, {})
        self.assertIsNone(model)

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_non_integer_line_fails_open(self, mock_call):
        mock_call.return_value = "9\ngreat\n1"
        scores, model = deal_scorer.score_deals([_make_deal(i) for i in (1, 2, 3)])
        self.assertEqual(scores, {})
        self.assertIsNone(model)

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_prompt_carries_the_deals(self, mock_call):
        mock_call.return_value = "9\n10"
        deals = [_make_deal(1), _make_deal(2)]

        deal_scorer.score_deals(deals)

        sent_user_prompt = mock_call.call_args[0][2]
        self.assertIn("Deal 1", sent_user_prompt)
        self.assertIn("Deal 2", sent_user_prompt)

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_reasoning_is_omitted_for_gemma(self, mock_call):
        # Empirical finding: Gemma 4 26B burns its token budget when any
        # reasoning effort is set. Lock that in so it isn't silently
        # reintroduced — the opposite of the caption/classifier models.
        mock_call.return_value = "9"
        deal_scorer.score_deals([_make_deal(1)])
        self.assertNotIn("reasoning", mock_call.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()