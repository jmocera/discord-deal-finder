"""Tests for Discord webhook delivery (_post_webhook) — rate-limit handling,
especially the non-JSON-429 crash path. Stdlib only (unittest + mock); every
HTTP call is mocked.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot.integrations import discord


def _resp(status: int, *, json_data=None, json_raises=False, text="") -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.text = text
    if json_raises:
        resp.json.side_effect = ValueError("not JSON")
    else:
        resp.json.return_value = json_data if json_data is not None else {}
    return resp


class PostWebhookRateLimitTests(unittest.TestCase):
    def setUp(self):
        self._sleep = patch("deal_bot.integrations.discord.time.sleep").start()

    def tearDown(self):
        patch.stopall()

    @patch("deal_bot.integrations.discord.requests.post")
    def test_429_non_json_body_falls_back_to_1s(self, mock_post):
        # Cloudflare-style HTML 429: .json() raises. Must retry, not crash
        # the posting loop with an uncaught JSONDecodeError.
        mock_post.side_effect = [
            _resp(429, json_raises=True),
            _resp(204),
        ]
        ok = discord._post_webhook("https://discord/hook", {}, "test")
        self.assertTrue(ok)
        self.assertAlmostEqual(self._sleep.call_args.args[0], 1.25)

    @patch("deal_bot.integrations.discord.requests.post")
    def test_429_json_retry_after_honored(self, mock_post):
        mock_post.side_effect = [
            _resp(429, json_data={"retry_after": 0.5}),
            _resp(204),
        ]
        ok = discord._post_webhook("https://discord/hook", {}, "test")
        self.assertTrue(ok)
        self.assertAlmostEqual(self._sleep.call_args.args[0], 0.75)

    @patch("deal_bot.integrations.discord.requests.post")
    def test_429_string_retry_after_coerced(self, mock_post):
        mock_post.side_effect = [
            _resp(429, json_data={"retry_after": "0.5"}),
            _resp(204),
        ]
        ok = discord._post_webhook("https://discord/hook", {}, "test")
        self.assertTrue(ok)
        self.assertAlmostEqual(self._sleep.call_args.args[0], 0.75)

    @patch("deal_bot.integrations.discord.requests.post")
    def test_429_missing_key_defaults_to_1s(self, mock_post):
        mock_post.side_effect = [
            _resp(429, json_data={}),
            _resp(204),
        ]
        ok = discord._post_webhook("https://discord/hook", {}, "test")
        self.assertTrue(ok)
        self.assertAlmostEqual(self._sleep.call_args.args[0], 1.25)


if __name__ == "__main__":
    unittest.main()
