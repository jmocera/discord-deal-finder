"""Tests for the phased posting pipeline (pipeline.py) — the deterministic
filter predicate and the batched AI-enrichment fallbacks.

Stdlib only (unittest + unittest.mock). Every network / AI call is mocked —
no real Supabase/OpenRouter/Discord/Bluesky traffic.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot import config
from deal_bot import pipeline
from deal_bot.ai import deal_analyst, spec_extraction


def _deal(**overrides) -> dict:
    deal = {
        "id": "woot:test-1", "source": "Woot", "title": "Some Deal",
        "url": "https://example.com/deal", "image": None,
        "sale_price": 30.0, "list_price": 60.0, "discount_pct": 50.0,
    }
    deal.update(overrides)
    return deal


class RunOnceBailTests(unittest.TestCase):
    """run_once must bail (log + return) when load_seen fails hard, WITHOUT
    fetching any feeds — running on an empty seen map risks double-posts."""

    @patch("deal_bot.pipeline.load_seen", return_value=None)
    @patch("deal_bot.pipeline.log_run")
    def test_load_seen_none_bails_and_logs(self, mock_log, mock_load):
        with patch("deal_bot.pipeline.fetch_woot_feed") as mock_woot, \
             patch("deal_bot.pipeline.fetch_steam_specials") as mock_steam:
            pipeline.run_once()

        mock_woot.assert_not_called()
        mock_steam.assert_not_called()
        self.assertEqual(mock_log.call_count, 1)
        self.assertIn("load_seen failed", mock_log.call_args.kwargs["error"])

    @patch("deal_bot.pipeline.time.sleep")
    @patch("deal_bot.pipeline.load_seen", return_value={})
    @patch("deal_bot.pipeline.log_run")
    def test_empty_seen_proceeds_to_feeds(self, mock_log, mock_load, mock_sleep):
        # {} means "no Supabase config / nothing seen" — not a failure, so the
        # run proceeds to fetching feeds. time.sleep is mocked so the per-feed
        # rate-limit sleeps don't slow the test.
        with patch("deal_bot.pipeline.fetch_woot_feed", return_value=[]), \
             patch("deal_bot.pipeline.fetch_bestbuy_search", return_value=[]), \
             patch("deal_bot.pipeline.fetch_steam_specials", return_value=[]), \
             patch("deal_bot.pipeline._process_deals"), \
             patch("deal_bot.pipeline.record_price_observations"), \
             patch("deal_bot.pipeline.get_price_history_stats_bulk", return_value={}):
            pipeline.run_once()

        self.assertEqual(mock_log.call_count, 1)  # a normal completion log_run


class SkipReasonTests(unittest.TestCase):
    def setUp(self):
        # Save originals and pin a deterministic baseline so the tests are
        # independent of .env values AND of any config mutated by an earlier
        # test module (tearDown restores exactly what setUp saved).
        self._orig = (
            config.MIN_DISCOUNT_PERCENT,
            config.MIN_DOLLAR_SAVINGS,
            config.PRICE_HISTORY_MIN_DAYS,
            config.PRICE_HISTORY_TOLERANCE_PERCENT,
        )
        config.MIN_DISCOUNT_PERCENT = 20
        config.MIN_DOLLAR_SAVINGS = 10
        config.PRICE_HISTORY_MIN_DAYS = 3
        config.PRICE_HISTORY_TOLERANCE_PERCENT = 5

    def tearDown(self):
        (config.MIN_DISCOUNT_PERCENT, config.MIN_DOLLAR_SAVINGS,
         config.PRICE_HISTORY_MIN_DAYS, config.PRICE_HISTORY_TOLERANCE_PERCENT) = self._orig

    def test_new_deal_passes(self):
        self.assertIsNone(pipeline._skip_reason(_deal(), None, 0, None))

    def test_already_seen_with_no_price(self):
        prior = {"sale_price": None}
        self.assertEqual(pipeline._skip_reason(_deal(), prior, 0, None), "skipped_already_seen")

    def test_not_enough_better_than_prior(self):
        prior = {"sale_price": 25.0}
        # deal at 30 vs prior 25: 30 >= 25 - 10 => skip
        self.assertEqual(pipeline._skip_reason(_deal(), prior, 0, None), "skipped_no_better_price")

    def test_below_discount_percent(self):
        config.MIN_DISCOUNT_PERCENT = 20
        self.assertEqual(pipeline._skip_reason(_deal(discount_pct=10), None, 0, None), "skipped_below_threshold")

    def test_below_dollar_savings(self):
        config.MIN_DOLLAR_SAVINGS = 50
        # list 60 - sale 30 = 30 < 50
        self.assertEqual(pipeline._skip_reason(_deal(), None, 0, None), "skipped_below_threshold")

    def test_above_historical_floor(self):
        config.PRICE_HISTORY_MIN_DAYS = 3
        config.PRICE_HISTORY_TOLERANCE_PERCENT = 5
        # floor 20, tolerance 5% -> ceiling 21; sale 30 > 21
        self.assertEqual(pipeline._skip_reason(_deal(), None, 5, 20.0), "skipped_not_near_historical_low")

    def test_near_historical_floor_passes(self):
        config.PRICE_HISTORY_MIN_DAYS = 3
        config.PRICE_HISTORY_TOLERANCE_PERCENT = 5
        # low 29, ceiling 30.45; sale 30 ok
        self.assertIsNone(pipeline._skip_reason(_deal(), None, 5, 29.0))

    def test_history_insufficient_is_dormant(self):
        config.PRICE_HISTORY_MIN_DAYS = 3
        self.assertIsNone(pipeline._skip_reason(_deal(), None, 1, 20.0))


class BatchSpecExtractionTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    def test_empty_list_returns_empty(self):
        self.assertEqual(spec_extraction.extract_clean_specs_batch([]), [])

    def test_no_api_key_returns_raw_titles(self):
        config.OPENROUTER_API_KEY = ""
        with patch("deal_bot.ai.spec_extraction._call_openrouter") as mock_call:
            result = spec_extraction.extract_clean_specs_batch(["A", "B"])
            mock_call.assert_not_called()
        self.assertEqual(result, [{"clean_title": "A", "specs": []}, {"clean_title": "B", "specs": []}])

    @patch("deal_bot.ai.spec_extraction._call_openrouter")
    def test_valid_batch_returns_items(self, mock_call):
        mock_call.return_value = '{"items": [{"clean_title": "A Title", "specs": ["Cap: 1TB"]}, {"clean_title": "B Title", "specs": []}]}'
        result = spec_extraction.extract_clean_specs_batch(["A", "B"])
        self.assertEqual(result[0]["clean_title"], "A Title")
        self.assertEqual(result[1]["specs"], [])

    @patch("deal_bot.ai.spec_extraction._call_openrouter")
    def test_wrong_item_count_falls_back_per_item(self, mock_call):
        # Batch returns only 1 item for 2 titles -> fall back to per-item
        mock_call.side_effect = [
            '{"items": [{"clean_title": "Only", "specs": []}]}',  # batch attempt
            '{"clean_title": "A Real", "specs": []}',  # per-item 1
            '{"clean_title": "B Real", "specs": []}',  # per-item 2
        ]
        result = spec_extraction.extract_clean_specs_batch(["A", "B"])
        self.assertEqual(result[0]["clean_title"], "A Real")
        self.assertEqual(result[1]["clean_title"], "B Real")

    @patch("deal_bot.ai.spec_extraction._call_openrouter")
    def test_invalid_item_type_falls_back_per_item(self, mock_call):
        mock_call.side_effect = [
            '{"items": "not-a-list"}',  # batch attempt
            '{"clean_title": "A Real", "specs": []}',  # per-item
        ]
        result = spec_extraction.extract_clean_specs_batch(["A"])
        self.assertEqual(result[0]["clean_title"], "A Real")


class BatchAnalysisTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    def test_empty_returns_empty(self):
        self.assertEqual(deal_analyst.build_ai_analysis_batch([]), [])

    @patch("deal_bot.ai.deal_analyst._call_openrouter")
    def test_valid_batch_returns_items(self, mock_call):
        mock_call.return_value = '{"items": ["Analysis for 1", "Analysis for 2"]}'
        deals = [_deal(id="woot:1"), _deal(id="woot:2")]
        result = deal_analyst.build_ai_analysis_batch(deals)
        self.assertEqual(result, ["Analysis for 1", "Analysis for 2"])

    @patch("deal_bot.ai.deal_analyst._call_openrouter")
    def test_wrong_count_falls_back_per_item(self, mock_call):
        # Batch loop tries both models (primary + fallback); both return a
        # wrong-count batch, then the per-item fallback runs per deal.
        mock_call.side_effect = [
            '{"items": ["Only one"]}',  # primary batch (1 for 2 deals)
            '{"items": ["Only one"]}',  # fallback batch (also wrong)
            "Analysis for deal 1",  # per-item 1
            "Analysis for deal 2",  # per-item 2
        ]
        deals = [_deal(id="woot:1"), _deal(id="woot:2")]
        result = deal_analyst.build_ai_analysis_batch(deals)
        self.assertEqual(result, ["Analysis for deal 1", "Analysis for deal 2"])

    @patch("deal_bot.ai.deal_analyst._call_openrouter")
    def test_overlength_item_falls_back_per_item(self, mock_call):
        mock_call.side_effect = [
            '{"items": ["x" * 500]}',  # both batch attempts overlength
            '{"items": ["x" * 500]}',
            "Short analysis",
        ]
        result = deal_analyst.build_ai_analysis_batch([_deal()])
        self.assertEqual(result, ["Short analysis"])


if __name__ == "__main__":
    unittest.main()