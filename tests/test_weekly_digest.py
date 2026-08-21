"""Tests for the weekly digest (weekly_digest.py) and the posted_deals log.

Stdlib only (unittest + unittest.mock). Every network call is mocked — no
real Supabase/OpenRouter/Discord/Bluesky traffic.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot import config
from deal_bot import weekly_digest
from deal_bot.storage import supabase


def _posted(i: int) -> dict:
    return {
        "id": f"woot:test-{i}", "source": "Woot", "title": f"Deal {i}",
        "url": "https://example.com/deal", "sale_price": 50.0, "list_price": 100.0,
    }


class BuildWeeklyDigestTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    def test_no_api_key_returns_empty(self):
        config.OPENROUTER_API_KEY = ""
        with patch("deal_bot.weekly_digest._call_openrouter") as mock_call:
            result = weekly_digest.build_weekly_digest([_posted(1)])
            mock_call.assert_not_called()
        self.assertEqual(result, "")

    def test_no_deals_returns_empty(self):
        self.assertEqual(weekly_digest.build_weekly_digest([]), "")

    @patch("deal_bot.weekly_digest._call_openrouter")
    def test_returns_text_on_success(self, mock_call):
        mock_call.return_value = "This week's best PC and gaming deals: ..."
        result = weekly_digest.build_weekly_digest([_posted(1)])
        self.assertEqual(result, "This week's best PC and gaming deals: ...")

    @patch("deal_bot.weekly_digest._call_openrouter")
    def test_returns_empty_when_both_models_fail(self, mock_call):
        mock_call.return_value = None
        result = weekly_digest.build_weekly_digest([_posted(1)])
        self.assertEqual(result, "")
        self.assertEqual(mock_call.call_count, 2)

    @patch("deal_bot.weekly_digest._call_openrouter")
    def test_prompt_carries_titles_and_discount(self, mock_call):
        mock_call.return_value = "roundup"
        weekly_digest.build_weekly_digest([_posted(1)])
        sent_user_prompt = mock_call.call_args[0][2]
        self.assertIn("Deal 1", sent_user_prompt)
        self.assertIn("50.0% off", sent_user_prompt)


class FetchRecentPostedTests(unittest.TestCase):
    def test_no_supabase_config_returns_empty(self):
        with patch.object(config, "SUPABASE_URL", ""):
            self.assertEqual(weekly_digest.fetch_recent_posted(), [])

    @patch("deal_bot.weekly_digest.requests.get")
    def test_non_200_returns_empty(self, mock_get):
        resp = Mock()
        resp.status_code = 404
        resp.text = "not found"
        mock_get.return_value = resp
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            self.assertEqual(weekly_digest.fetch_recent_posted(), [])


class RecordPostedDealTests(unittest.TestCase):
    def test_no_supabase_config_is_a_noop(self):
        with patch.object(config, "SUPABASE_URL", ""):
            with patch("deal_bot.storage.supabase.requests.post") as mock_post:
                supabase.record_posted_deal(_posted(1))
                mock_post.assert_not_called()

    @patch("deal_bot.storage.supabase.requests.post")
    def test_missing_table_fails_silent(self, mock_post):
        resp = Mock()
        resp.status_code = 404
        resp.text = "table does not exist"
        mock_post.return_value = resp
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            supabase.record_posted_deal(_posted(1))  # must not raise
        mock_post.assert_called_once()


class PrunePostedDealsTests(unittest.TestCase):
    def test_no_supabase_config_is_a_noop(self):
        with patch.object(config, "SUPABASE_URL", ""):
            with patch("deal_bot.weekly_digest.requests.delete") as mock_delete:
                weekly_digest.prune_posted_deals()
                mock_delete.assert_not_called()

    @patch("deal_bot.weekly_digest.requests.delete")
    def test_prunes_with_cutoff(self, mock_delete):
        resp = Mock()
        resp.status_code = 204
        mock_delete.return_value = resp
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            weekly_digest.prune_posted_deals(ttl_days=90)
        self.assertIn("posted_at", mock_delete.call_args.kwargs["params"])


class SeedClearPostedDealsTests(unittest.TestCase):
    def test_seed_no_supabase_config_does_nothing(self):
        with patch.object(config, "SUPABASE_URL", ""):
            with patch("deal_bot.weekly_digest.requests.post") as mock_post:
                weekly_digest.seed_posted_deals(3)
                mock_post.assert_not_called()

    @patch("deal_bot.weekly_digest.requests.post")
    def test_seed_posts_rows(self, mock_post):
        resp = Mock()
        resp.status_code = 201
        mock_post.return_value = resp
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            weekly_digest.seed_posted_deals(3)
        sent = mock_post.call_args.kwargs["json"]
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(r["id"].startswith("seed:") for r in sent))

    def test_clear_no_supabase_config_does_nothing(self):
        with patch.object(config, "SUPABASE_URL", ""):
            with patch("deal_bot.weekly_digest.requests.delete") as mock_delete:
                weekly_digest.clear_posted_deals()
                mock_delete.assert_not_called()

    @patch("deal_bot.weekly_digest.requests.delete")
    def test_clear_deletes_all(self, mock_delete):
        resp = Mock()
        resp.status_code = 204
        mock_delete.return_value = resp
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            weekly_digest.clear_posted_deals()
        mock_delete.assert_called_once()


class RunWeeklyDigestTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    @patch("deal_bot.weekly_digest._call_openrouter")
    @patch("deal_bot.weekly_digest.fetch_recent_posted")
    def test_dry_run_does_not_post(self, mock_fetch, mock_call):
        mock_fetch.return_value = [_posted(1)]
        mock_call.return_value = "Some roundup text."

        with patch("deal_bot.weekly_digest._post_webhook") as mock_webhook, \
             patch("deal_bot.weekly_digest.post_text_to_bluesky") as mock_bsky:
            result = weekly_digest.run_weekly_digest(dry_run=True)

        self.assertTrue(result)
        mock_webhook.assert_not_called()
        mock_bsky.assert_not_called()

    @patch("deal_bot.weekly_digest._call_openrouter")
    @patch("deal_bot.weekly_digest.fetch_recent_posted")
    def test_skip_bluesky_skips_only_bluesky(self, mock_fetch, mock_call):
        mock_fetch.return_value = [_posted(1)]
        mock_call.return_value = "Some roundup text."

        with patch("deal_bot.weekly_digest._post_webhook") as mock_webhook, \
             patch("deal_bot.weekly_digest.post_text_to_bluesky") as mock_bsky:
            with patch.object(config, "DIGEST_WEBHOOK_URL", "https://discord/x"), \
                 patch.object(config, "BLUESKY_HANDLE", "h"), \
                 patch.object(config, "BLUESKY_APP_PASSWORD", "p"):
                result = weekly_digest.run_weekly_digest(skip_bluesky=True)

        self.assertTrue(result)
        mock_webhook.assert_called_once()
        mock_bsky.assert_not_called()


if __name__ == "__main__":
    unittest.main()