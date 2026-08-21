"""Tests for the weekly digest (weekly_digest.py) and the posted_deals log.

Stdlib only (unittest + unittest.mock). Every network call is mocked — no
real Supabase/OpenRouter/Discord/Bluesky traffic.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot import config, transport
from deal_bot import weekly_digest
from deal_bot.storage import supabase


def _posted(i: int) -> dict:
    return {
        "id": f"woot:test-{i}", "source": "Woot", "title": f"Deal {i}",
        "url": "https://example.com/deal", "sale_price": 50.0, "list_price": 100.0,
    }


def _resp(status: int) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.text = f"status {status}"
    resp.headers = {}
    resp.json.return_value = [_posted(1)]
    return resp


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


class SupabaseRequestRetryTests(unittest.TestCase):
    """Retry/backoff behavior of the shared transport helper, exercised via
    the weekly_digest Supabase wrapper. Patches the INNER requests.request and
    time.sleep so the real retry loop runs."""

    @patch("deal_bot.transport.time.sleep")
    @patch("deal_bot.transport.requests.request")
    def test_success_on_first_try_does_not_sleep(self, mock_net, mock_sleep):
        mock_net.return_value = _resp(200)
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            resp = weekly_digest._supabase_request("GET", "https://x.supabase.co/rest/v1/posted_deals")
        self.assertIsNotNone(resp)
        self.assertEqual(mock_net.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("deal_bot.transport.time.sleep")
    @patch("deal_bot.transport.requests.request")
    def test_retries_transient_then_succeeds(self, mock_net, mock_sleep):
        mock_net.side_effect = [_resp(503), _resp(200)]
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            resp = weekly_digest._supabase_request("GET", "https://x.supabase.co/rest/v1/posted_deals")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_net.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @patch("deal_bot.transport.time.sleep")
    @patch("deal_bot.transport.requests.request")
    def test_exhausts_retries_on_network_error(self, mock_net, mock_sleep):
        mock_net.side_effect = [requests.RequestException("net")] * 3
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            resp = weekly_digest._supabase_request("GET", "https://x.supabase.co/rest/v1/posted_deals")
        self.assertIsNone(resp)
        self.assertEqual(mock_net.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("deal_bot.transport.time.sleep")
    @patch("deal_bot.transport.requests.request")
    def test_permanent_4xx_is_not_retried(self, mock_net, mock_sleep):
        mock_net.return_value = _resp(404)
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            resp = weekly_digest._supabase_request("GET", "https://x.supabase.co/rest/v1/posted_deals")
        self.assertIsNotNone(resp)  # the 404 response is returned as-is
        self.assertEqual(mock_net.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("deal_bot.transport.time.sleep")
    @patch("deal_bot.transport.requests.request")
    def test_retry_after_is_capped(self, mock_net, mock_sleep):
        # A huge Retry-After (e.g. 3600s) must be clamped so "bounded"
        # backoff stays bounded.
        resp429 = _resp(429)
        resp429.headers = {"Retry-After": "3600"}
        mock_net.side_effect = [resp429, _resp(200)]
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            resp = weekly_digest._supabase_request("GET", "https://x.supabase.co/rest/v1/posted_deals")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_net.call_count, 2)
        slept = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertTrue(all(w <= transport.MAX_SLEEP_SECONDS for w in slept))


class FetchRecentPostedTests(unittest.TestCase):
    def test_no_supabase_config_returns_empty(self):
        with patch.object(config, "SUPABASE_URL", ""):
            self.assertEqual(weekly_digest.fetch_recent_posted(), [])

    @patch("deal_bot.weekly_digest._supabase_request")
    def test_non_200_returns_none(self, mock_req):
        mock_req.return_value = _resp(404)
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            self.assertIsNone(weekly_digest.fetch_recent_posted())

    @patch("deal_bot.weekly_digest._supabase_request")
    def test_network_failure_returns_none(self, mock_req):
        mock_req.return_value = None
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            self.assertIsNone(weekly_digest.fetch_recent_posted())

    @patch("deal_bot.weekly_digest._supabase_request")
    def test_success_returns_rows(self, mock_req):
        mock_req.return_value = _resp(200)
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            result = weekly_digest.fetch_recent_posted()
        self.assertEqual(len(result), 1)


class RecordPostedDealTests(unittest.TestCase):
    def test_no_supabase_config_is_a_noop(self):
        with patch.object(config, "SUPABASE_URL", ""):
            with patch("deal_bot.storage.supabase.transport.request") as mock_req:
                supabase.record_posted_deal(_posted(1))
                mock_req.assert_not_called()

    @patch("deal_bot.storage.supabase.transport.request")
    def test_missing_table_fails_silent(self, mock_req):
        resp = Mock()
        resp.status_code = 404
        resp.text = "table does not exist"
        mock_req.return_value = resp
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            supabase.record_posted_deal(_posted(1))  # must not raise
        mock_req.assert_called_once()


class PrunePostedDealsTests(unittest.TestCase):
    def test_no_supabase_config_is_a_noop(self):
        with patch.object(config, "SUPABASE_URL", ""):
            with patch("deal_bot.weekly_digest._supabase_request") as mock_req:
                weekly_digest.prune_posted_deals()
                mock_req.assert_not_called()

    @patch("deal_bot.weekly_digest._supabase_request")
    def test_prunes_with_cutoff(self, mock_req):
        mock_req.return_value = _resp(204)
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            weekly_digest.prune_posted_deals(ttl_days=90)
        self.assertIn("posted_at", mock_req.call_args.kwargs["params"])


class SeedClearPostedDealsTests(unittest.TestCase):
    def test_seed_no_supabase_config_does_nothing(self):
        with patch.object(config, "SUPABASE_URL", ""):
            with patch("deal_bot.weekly_digest._supabase_request") as mock_req:
                weekly_digest.seed_posted_deals(3)
                mock_req.assert_not_called()

    @patch("deal_bot.weekly_digest._supabase_request")
    def test_seed_posts_rows(self, mock_req):
        mock_req.return_value = _resp(201)
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            weekly_digest.seed_posted_deals(3)
        sent = mock_req.call_args.kwargs["json"]
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(r["id"].startswith("seed:") for r in sent))

    def test_clear_no_supabase_config_does_nothing(self):
        with patch.object(config, "SUPABASE_URL", ""):
            with patch("deal_bot.weekly_digest._supabase_request") as mock_req:
                weekly_digest.clear_posted_deals()
                mock_req.assert_not_called()

    @patch("deal_bot.weekly_digest._supabase_request")
    def test_clear_deletes_seeded_rows_only(self, mock_req):
        mock_req.return_value = _resp(204)
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            weekly_digest.clear_posted_deals()
        mock_req.assert_called_once()
        # PostgREST requires a WHERE clause; scope to the seed prefix so the
        # whole table is never wiped.
        self.assertEqual(mock_req.call_args.kwargs["params"]["id"], "like.seed:%")


class RunWeeklyDigestTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.OPENROUTER_API_KEY
        config.OPENROUTER_API_KEY = "test-key"

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig_key

    @patch("deal_bot.weekly_digest.fetch_recent_posted")
    def test_dry_run_returns_true_and_posts_nothing(self, mock_fetch):
        mock_fetch.return_value = [_posted(1)]

        with patch("deal_bot.weekly_digest._call_openrouter") as mock_call, \
             patch("deal_bot.weekly_digest._post_webhook") as mock_webhook, \
             patch("deal_bot.weekly_digest.post_text_to_bluesky") as mock_bsky, \
             patch.object(config, "DIGEST_WEBHOOK_URL", "https://discord/x"), \
             patch.object(config, "BLUESKY_HANDLE", "h"), \
             patch.object(config, "BLUESKY_APP_PASSWORD", "p"):
            mock_call.return_value = "Some roundup text."
            result = weekly_digest.run_weekly_digest(dry_run=True)

        self.assertTrue(result)
        mock_webhook.assert_not_called()
        mock_bsky.assert_not_called()

    @patch("deal_bot.weekly_digest.fetch_recent_posted")
    def test_skip_bluesky_skips_only_bluesky(self, mock_fetch):
        mock_fetch.return_value = [_posted(1)]

        with patch("deal_bot.weekly_digest._call_openrouter") as mock_call, \
             patch("deal_bot.weekly_digest._post_webhook") as mock_webhook, \
             patch("deal_bot.weekly_digest.post_text_to_bluesky") as mock_bsky, \
             patch.object(config, "DIGEST_WEBHOOK_URL", "https://discord/x"), \
             patch.object(config, "BLUESKY_HANDLE", "h"), \
             patch.object(config, "BLUESKY_APP_PASSWORD", "p"):
            mock_call.return_value = "Some roundup text."
            result = weekly_digest.run_weekly_digest(skip_bluesky=True)

        self.assertTrue(result)
        mock_webhook.assert_called_once()
        mock_bsky.assert_not_called()

    @patch("deal_bot.weekly_digest.fetch_recent_posted")
    def test_fetch_failure_returns_false(self, mock_fetch):
        mock_fetch.return_value = None
        self.assertIs(weekly_digest.run_weekly_digest(), False)

    @patch("deal_bot.weekly_digest.fetch_recent_posted")
    def test_no_deals_returns_none(self, mock_fetch):
        mock_fetch.return_value = []
        self.assertIsNone(weekly_digest.run_weekly_digest())

    @patch("deal_bot.weekly_digest.fetch_recent_posted")
    def test_both_models_fail_returns_false(self, mock_fetch):
        mock_fetch.return_value = [_posted(1)]
        with patch("deal_bot.weekly_digest._call_openrouter") as mock_call:
            mock_call.return_value = None
            self.assertIs(weekly_digest.run_weekly_digest(), False)

    @patch("deal_bot.weekly_digest.fetch_recent_posted")
    def test_nothing_delivered_returns_false(self, mock_fetch):
        mock_fetch.return_value = [_posted(1)]
        with patch("deal_bot.weekly_digest._call_openrouter") as mock_call, \
             patch("deal_bot.weekly_digest._post_webhook", return_value=False) as mock_webhook, \
             patch("deal_bot.weekly_digest.post_text_to_bluesky", return_value=False) as mock_bsky, \
             patch.object(config, "DIGEST_WEBHOOK_URL", "https://discord/x"), \
             patch.object(config, "BLUESKY_HANDLE", "h"), \
             patch.object(config, "BLUESKY_APP_PASSWORD", "p"):
            mock_call.return_value = "Some roundup text."
            result = weekly_digest.run_weekly_digest()

        self.assertIs(result, False)
        mock_webhook.assert_called_once()
        mock_bsky.assert_called_once()


class ExitCodeTests(unittest.TestCase):
    def test_delivered_is_zero(self):
        self.assertEqual(weekly_digest._exit_code(True), 0)

    def test_skipped_is_zero(self):
        self.assertEqual(weekly_digest._exit_code(None), 0)

    def test_failed_is_one(self):
        self.assertEqual(weekly_digest._exit_code(False), 1)


if __name__ == "__main__":
    unittest.main()