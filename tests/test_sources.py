"""Tests for the deal sources (woot/bestbuy/steam) — URL construction,
response mapping, and transport integration. Stdlib only; every HTTP call
is mocked at the shared transport boundary.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot import config
from deal_bot.sources import bestbuy, steam, woot


def _resp(status: int = 200, payload=None, text="") -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.text = text or f"status {status}"
    resp.json.return_value = payload if payload is not None else {}
    return resp


class WootTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.WOOT_API_KEY
        config.WOOT_API_KEY = "test-key"

    def tearDown(self):
        config.WOOT_API_KEY = self._orig_key

    @patch("deal_bot.sources.woot.transport.request")
    def test_valid_item_maps_to_deal(self, mock_req):
        mock_req.return_value = _resp(payload={"Items": [{
            "OfferId": "abc123", "Title": "Dell 27in Monitor 144Hz",
            "Url": "https://woot/x", "Photo": "https://img/x.jpg",
            "IsSoldOut": False, "Categories": ["ELECTRONICS"],
            "SalePrice": {"Minimum": 99.0}, "ListPrice": {"Minimum": 199.0},
        }]})
        deals = woot.fetch_woot_feed("Electronics")
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0]["id"], "woot:abc123")
        self.assertEqual(deals[0]["sale_price"], 99.0)
        self.assertEqual(deals[0]["discount_pct"], 50.3)  # round((199-99)/199*100, 1)

    @patch("deal_bot.sources.woot.transport.request")
    def test_network_exhaustion_returns_empty_list(self, mock_req):
        mock_req.return_value = None
        self.assertEqual(woot.fetch_woot_feed("Electronics"), [])

    @patch("deal_bot.sources.woot.transport.request")
    def test_non_200_returns_empty_list(self, mock_req):
        mock_req.return_value = _resp(status=500, text="boom")
        self.assertEqual(woot.fetch_woot_feed("Electronics"), [])


class BestBuyTests(unittest.TestCase):
    def setUp(self):
        self._orig_key = config.BESTBUY_API_KEY
        config.BESTBUY_API_KEY = "secret-key"

    def tearDown(self):
        config.BESTBUY_API_KEY = self._orig_key

    @patch("deal_bot.sources.bestbuy.transport.request")
    def test_query_encodes_only_term(self, mock_req):
        # Regression: quote() used to encode the structural '&'/'=' too.
        mock_req.return_value = _resp(payload={"products": []})
        bestbuy.fetch_bestbuy_search("gaming mouse")
        url = mock_req.call_args.args[1]
        self.assertIn("(search=gaming%20mouse&onSale=true)", url)
        self.assertNotIn("%26", url)
        self.assertNotIn("%3D", url)

    @patch("deal_bot.sources.bestbuy.transport.request")
    def test_ampersand_in_term_is_encoded(self, mock_req):
        mock_req.return_value = _resp(payload={"products": []})
        bestbuy.fetch_bestbuy_search("A&B")
        url = mock_req.call_args.args[1]
        self.assertIn("(search=A%26B&onSale=true)", url)

    @patch("deal_bot.sources.bestbuy.transport.request")
    def test_url_carries_api_key_and_params_in_order(self, mock_req):
        mock_req.return_value = _resp(payload={"products": []})
        bestbuy.fetch_bestbuy_search("ssd")
        url = mock_req.call_args.args[1]
        self.assertTrue(url.startswith("https://api.bestbuy.com/v1/products(search=ssd&onSale=true)?"))
        self.assertIn("apiKey=secret-key&format=json&show=", url)
        self.assertIn("&pageSize=20", url)

    @patch("deal_bot.sources.bestbuy.transport.request")
    def test_product_mapping(self, mock_req):
        mock_req.return_value = _resp(payload={"products": [{
            "sku": 65092, "name": "WD SSD", "salePrice": 79.99,
            "regularPrice": 159.99, "url": "/p/65092", "image": "i.jpg",
        }]})
        deals = bestbuy.fetch_bestbuy_search("ssd")
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0]["id"], "bestbuy:65092")
        self.assertAlmostEqual(deals[0]["discount_pct"], 50.0)

    @patch("deal_bot.sources.bestbuy.transport.request")
    def test_network_exhaustion_returns_empty_list(self, mock_req):
        mock_req.return_value = None
        self.assertEqual(bestbuy.fetch_bestbuy_search("ssd"), [])

    @patch("deal_bot.sources.bestbuy.transport.request")
    def test_error_text_is_redacted(self, mock_req):
        # The key travels in the URL; an error body echoing it must be
        # redacted before hitting logs/CI output.
        mock_req.return_value = _resp(status=403, text="key secret-key forbidden")
        with patch("builtins.print") as mock_print:
            self.assertEqual(bestbuy.fetch_bestbuy_search("ssd"), [])
        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertNotIn("secret-key", printed)
        self.assertIn("[REDACTED]", printed)


class SteamTests(unittest.TestCase):
    @patch("deal_bot.sources.steam.transport.request")
    def test_prices_converted_from_cents(self, mock_req):
        mock_req.return_value = _resp(payload={"specials": {"items": [{
            "id": 570, "name": "Dota 2", "final_price": 999,
            "original_price": 2999, "discount_percent": 67,
            "large_capsule_image": "https://img/capsule.jpg",
        }]}})
        deals = steam.fetch_steam_specials()
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0]["id"], "steam:570")
        self.assertEqual(deals[0]["sale_price"], 9.99)
        self.assertEqual(deals[0]["list_price"], 29.99)

    @patch("deal_bot.sources.steam.transport.request")
    def test_network_exhaustion_returns_empty_list(self, mock_req):
        mock_req.return_value = None
        self.assertEqual(steam.fetch_steam_specials(), [])

    @patch("deal_bot.sources.steam.transport.request")
    def test_non_200_returns_empty_list(self, mock_req):
        mock_req.return_value = _resp(status=503, text="busy")
        self.assertEqual(steam.fetch_steam_specials(), [])


if __name__ == "__main__":
    unittest.main()
