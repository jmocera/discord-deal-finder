"""Tests for the watchdog dead-man's switch (watchdog.py).

Stdlib only (unittest + unittest.mock). Every network call is mocked — no
real Supabase/Discord traffic.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot import config
from deal_bot import watchdog


def _resp(status: int, rows=None) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.text = f"status {status}"
    resp.json.return_value = rows or []
    return resp


class FetchLastRunTests(unittest.TestCase):
    # NOTE: config-gating moved OUT of fetch_last_run into run_watchdog —
    # see RunWatchdogTests.test_no_config_skips_alert_without_fetching.

    @patch("deal_bot.watchdog.transport.request")
    def test_network_failure_returns_none(self, mock_req):
        mock_req.return_value = None
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            self.assertIsNone(watchdog.fetch_last_run())

    @patch("deal_bot.watchdog.transport.request")
    def test_no_rows_returns_none(self, mock_req):
        mock_req.return_value = _resp(200, [])
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            self.assertIsNone(watchdog.fetch_last_run())

    @patch("deal_bot.watchdog.transport.request")
    def test_parses_ran_at(self, mock_req):
        mock_req.return_value = _resp(200, [{"ran_at": "2026-08-21T10:00:00+00:00"}])
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            dt = watchdog.fetch_last_run()
        self.assertIsNotNone(dt)

    @patch("deal_bot.watchdog.transport.request")
    def test_orders_by_ran_at_desc(self, mock_req):
        mock_req.return_value = _resp(200, [{"ran_at": "2026-08-21T10:00:00+00:00"}])
        with patch.object(config, "SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(config, "SUPABASE_SERVICE_KEY", "k"):
            watchdog.fetch_last_run()
        self.assertEqual(mock_req.call_args.kwargs["params"]["order"], "ran_at.desc")
        self.assertEqual(mock_req.call_args.kwargs["params"]["limit"], "1")


class RunIsStaleTests(unittest.TestCase):
    def test_no_run_is_stale(self):
        self.assertTrue(watchdog._run_is_stale(None, 6))

    def test_fresh_run_is_not_stale(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(watchdog._run_is_stale(now, 6))

    def test_old_run_is_stale(self):
        old = datetime.now(timezone.utc) - timedelta(hours=10)
        self.assertTrue(watchdog._run_is_stale(old, 6))


class RunWatchdogTests(unittest.TestCase):
    def setUp(self):
        # Pin Supabase config so run_watchdog's no-config gate passes
        # deterministically regardless of the local .env — the individual
        # tests then control behavior via the mocked fetch_last_run.
        self._orig = (config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
        config.SUPABASE_URL = "https://x.supabase.co"
        config.SUPABASE_SERVICE_KEY = "k"

    def tearDown(self):
        (config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY) = self._orig

    def test_no_config_skips_alert_without_fetching(self):
        # No Supabase config = the watchdog can't know anything; it must
        # NOT post a false "no run in 6h" alarm, and must not touch the DB.
        with patch.object(config, "SUPABASE_URL", ""), \
             patch.object(config, "SUPABASE_SERVICE_KEY", ""), \
             patch("deal_bot.watchdog.fetch_last_run") as mock_fetch, \
             patch("deal_bot.watchdog._post_webhook") as mock_webhook:
            result = watchdog.run_watchdog()
        self.assertFalse(result)
        mock_fetch.assert_not_called()
        mock_webhook.assert_not_called()

    @patch("deal_bot.watchdog.fetch_last_run")
    def test_fresh_run_posts_no_alert(self, mock_fetch):
        mock_fetch.return_value = datetime.now(timezone.utc)
        with patch("deal_bot.watchdog._post_webhook") as mock_webhook:
            result = watchdog.run_watchdog()
        self.assertFalse(result)
        mock_webhook.assert_not_called()

    @patch("deal_bot.watchdog.fetch_last_run")
    def test_stale_run_posts_alert(self, mock_fetch):
        mock_fetch.return_value = datetime.now(timezone.utc) - timedelta(hours=10)
        with patch("deal_bot.watchdog._post_webhook") as mock_webhook:
            with patch.object(config, "RUN_LOG_WEBHOOK_URL", "https://discord/x"):
                result = watchdog.run_watchdog()
        self.assertTrue(result)
        mock_webhook.assert_called_once()

    @patch("deal_bot.watchdog.fetch_last_run")
    def test_stale_run_without_webhook_still_reports_stale(self, mock_fetch):
        mock_fetch.return_value = None
        with patch.object(config, "RUN_LOG_WEBHOOK_URL", ""):
            result = watchdog.run_watchdog()
        self.assertTrue(result)  # stale, but no webhook to post to


if __name__ == "__main__":
    unittest.main()