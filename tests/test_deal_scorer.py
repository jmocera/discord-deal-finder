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
    def test_wrong_line_count_produces_partial_report(self, mock_call):
        mock_call.return_value = "9\n10"  # 2 lines for 3 deals
        scores, model = deal_scorer.score_deals([_make_deal(i) for i in (1, 2, 3)])
        self.assertEqual(scores, {})
        self.assertIsNone(model)
        self.assertEqual(mock_call.call_count, 2)  # primary + fallback (partial salvage removed)

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_out_of_range_score_skipped_in_partial_report(self, mock_call):
        mock_call.return_value = "9\n42\n1"  # 42 not in 1..10
        scores, model = deal_scorer.score_deals([_make_deal(i) for i in (1, 2, 3)])
        self.assertEqual(scores, {})
        self.assertIsNone(model)
        self.assertEqual(mock_call.call_count, 2)

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_non_integer_line_skipped_in_partial_report(self, mock_call):
        mock_call.return_value = "9\ngreat\n1"
        scores, model = deal_scorer.score_deals([_make_deal(i) for i in (1, 2, 3)])
        self.assertEqual(scores, {})
        self.assertIsNone(model)
        self.assertEqual(mock_call.call_count, 2)

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


class LenientParseTests(unittest.TestCase):
    """The strict per-line parse voids the whole report on any stray text
    (real Gemma behavior). The lenient line-anchored regex fallback
    salvages a report only when it yields EXACTLY len(deals) clean score
    lines — partial salvage is intentionally removed so a response can
    never silently mislabel a deal."""

    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_preamble_narration_is_ignored(self, mock_call):
        mock_call.return_value = "Here are my ratings:\n9\n10\n1\nDone."
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(model, config.OPENROUTER_QUALITY_SCORER_MODEL)
        self.assertEqual(scores, {"woot:test-1": 9, "woot:test-2": 10, "woot:test-3": 1})

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_trailing_explanation_is_ignored(self, mock_call):
        mock_call.return_value = "9\n8\n# this one is a steal"
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(scores, {})
        self.assertIsNone(model)
        self.assertEqual(mock_call.call_count, 2)

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_partial_scores_produce_partial_map(self, mock_call):
        mock_call.return_value = "7 only one dealt with"
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(scores, {})
        self.assertIsNone(model)
        self.assertEqual(mock_call.call_count, 2)

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_markdown_and_dashes_are_handled(self, mock_call):
        mock_call.return_value = "- 9\n- 10\n- 8"
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(scores, {"woot:test-1": 9, "woot:test-2": 10, "woot:test-3": 8})

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_no_digits_at_all_falls_through_to_next_model(self, mock_call):
        mock_call.side_effect = ["all great deals", "all great deals"]
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(scores, {})
        self.assertIsNone(model)
        self.assertEqual(mock_call.call_count, 2)

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_extract_scores_rejects_decimals_and_multidigit(self, mock_call):
        # "10" is valid, "100" must NOT match (multi-digit, not anchored),
        # and a decimal like "8.5" must not yield a fake 8 or 5. The
        # line-anchored regex rejects all three contaminated lines.
        self.assertEqual(deal_scorer._extract_scores("10\n100\n8.5\n7"), [10, 7])

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_slash_ten_is_single_token_and_not_double_counted(self, mock_call):
        # The optional `(?:\s*/\s*10)?` suffix consumes `/10` as part of
        # the same line — it must NOT be counted as a second score.
        mock_call.return_value = "9/10\n8/10\n7/10"
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(model, config.OPENROUTER_QUALITY_SCORER_MODEL)
        self.assertEqual(scores, {"woot:test-1": 9, "woot:test-2": 8, "woot:test-3": 7})

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_slash_ten_allows_whitespace_around_slash(self, mock_call):
        mock_call.return_value = "9 / 10\n8 / 10\n7 / 10"
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(model, config.OPENROUTER_QUALITY_SCORER_MODEL)
        self.assertEqual(scores, {"woot:test-1": 9, "woot:test-2": 8, "woot:test-3": 7})

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_numbered_lines_parse_cleanly(self, mock_call):
        mock_call.return_value = "1. 9\n2. 8\n3. 7"
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(model, config.OPENROUTER_QUALITY_SCORER_MODEL)
        self.assertEqual(scores, {"woot:test-1": 9, "woot:test-2": 8, "woot:test-3": 7})

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_context_preamble_heading_ignored(self, mock_call):
        # A bare-word heading line is rejected (no score on it), but the
        # three following score lines parse cleanly.
        mock_call.return_value = "Scores:\n9\n8\n7"
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(model, config.OPENROUTER_QUALITY_SCORER_MODEL)
        self.assertEqual(scores, {"woot:test-1": 9, "woot:test-2": 8, "woot:test-3": 7})

    @patch("deal_bot.ai.deal_scorer._call_openrouter")
    def test_contaminated_narration_rejects_not_mislabels(self, mock_call):
        # The real-world repro: every line carries both a deal description
        # AND a trailing score. The old greedy regex would lift "10", "9",
        # "3", "8" and silently map them onto the wrong deals. The new
        # line-anchored regex rejects every line (none is a bare score),
        # so the run fails OPEN instead of mislabeling.
        mock_call.return_value = (
            "1. WD 10TB Elements drive is a steal at $159.99. 9\n"
            "2. i7 gaming PC, great value. 3\n"
            "3. 4K monitor with good price. 8"
        )
        deals = [_make_deal(i) for i in (1, 2, 3)]

        scores, model = deal_scorer.score_deals(deals)

        self.assertEqual(scores, {})
        self.assertIsNone(model)
        self.assertEqual(mock_call.call_count, 2)

    def test_extract_scores_line_anchored_table(self):
        # Accepted: each line is a bare score (with optional prefix/suffix).
        accepted = {
            "9": [9],
            "10": [10],
            "0": [],            # 0 is not in [1-9] or "10"
            "11": [],            # multi-digit, not "10"
            "100": [],           # after "10" comes "0", not \s*$
            "9/10": [9],
            "10/10": [10],
            "9 / 10": [9],
            "- 9": [9],
            "• 9": [9],
            "1. 9": [9],
            "2) 9": [9],
            "3: 9": [9],
            "  9  ": [9],
            "9\n10": [9, 10],
            "9\n\n10": [9, 10],          # blank line yields no token
            "Scores:\n9\n8\n7": [9, 8, 7],
            "1. 9\n2. 10\n3. 8": [9, 10, 8],
            "10\n100\n8.5\n7": [10, 7],
        }
        # Rejected: contaminated narration that the old greedy regex
        # would have silently lifted scores out of.
        rejected = {
            "great": [],
            "8.5": [],
            "9.5": [],
            "1.9": [],            # no whitespace after the separator
            "16:9": [],           # prefix needs \s+ after ':', here it's '9'
            "9 only": [],
            "Game Controller": [],
            "WD 10TB": [],
            "$159.99": [],
            "storage only really sure about this one": [],
        }
        for response, expected in {**accepted, **rejected}.items():
            with self.subTest(response=response):
                self.assertEqual(
                    deal_scorer._extract_scores(response), expected,
                    f"response={response!r}",
                )


if __name__ == "__main__":
    unittest.main()
