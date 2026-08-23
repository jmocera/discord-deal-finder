"""Tests for the Bluesky integration's login session validation. Stdlib
only; every HTTP call is mocked. The global _bluesky_session cache is
reset around each test.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot import config
from deal_bot.integrations import bluesky


def _login_response(payload) -> Mock:
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


class BlueskyLoginTests(unittest.TestCase):
    def setUp(self):
        self._orig = (config.BLUESKY_HANDLE, config.BLUESKY_APP_PASSWORD)
        self._orig_session = bluesky._bluesky_session
        config.BLUESKY_HANDLE = "voltdrop.bsky.social"
        config.BLUESKY_APP_PASSWORD = "app-pass"
        bluesky._bluesky_session = None

    def tearDown(self):
        (config.BLUESKY_HANDLE, config.BLUESKY_APP_PASSWORD) = self._orig
        bluesky._bluesky_session = self._orig_session

    @patch("deal_bot.integrations.bluesky.requests.post")
    def test_valid_session_is_cached(self, mock_post):
        mock_post.return_value = _login_response({"accessJwt": "jwt", "did": "did:plc:abc"})
        session = bluesky._bluesky_login()
        self.assertEqual(session["did"], "did:plc:abc")
        self.assertEqual(bluesky._bluesky_session, session)

    @patch("deal_bot.integrations.bluesky.requests.post")
    def test_missing_did_not_cached(self, mock_post):
        mock_post.return_value = _login_response({"accessJwt": "jwt"})
        self.assertIsNone(bluesky._bluesky_login())
        self.assertIsNone(bluesky._bluesky_session)

    @patch("deal_bot.integrations.bluesky.requests.post")
    def test_empty_access_jwt_not_cached(self, mock_post):
        mock_post.return_value = _login_response({"accessJwt": "", "did": "did:plc:abc"})
        self.assertIsNone(bluesky._bluesky_login())
        self.assertIsNone(bluesky._bluesky_session)

    @patch("deal_bot.integrations.bluesky.requests.post")
    def test_non_string_jwt_not_cached(self, mock_post):
        mock_post.return_value = _login_response({"accessJwt": 12345, "did": "did:plc:abc"})
        self.assertIsNone(bluesky._bluesky_login())
        self.assertIsNone(bluesky._bluesky_session)

    @patch("deal_bot.integrations.bluesky.requests.post")
    def test_non_dict_response_not_cached(self, mock_post):
        mock_post.return_value = _login_response("not a dict")
        self.assertIsNone(bluesky._bluesky_login())
        self.assertIsNone(bluesky._bluesky_session)

    @patch("deal_bot.integrations.bluesky.requests.post")
    def test_second_call_reuses_cache_without_new_request(self, mock_post):
        mock_post.return_value = _login_response({"accessJwt": "jwt", "did": "did:plc:abc"})
        first = bluesky._bluesky_login()
        second = bluesky._bluesky_login()
        self.assertIs(first, second)
        mock_post.assert_called_once()

    def test_no_credentials_skips_network(self):
        with patch.object(config, "BLUESKY_HANDLE", ""), \
             patch("deal_bot.integrations.bluesky.requests.post") as mock_post:
            self.assertIsNone(bluesky._bluesky_login())
            mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
