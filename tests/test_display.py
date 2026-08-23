"""Tests for deal_bot.display — the shared price/discount formatting used
by captions, analysis prompts, embeds, and digest bullets.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot.display import discount_str, price_str


class PriceStrTests(unittest.TestCase):
    def test_no_list_price(self):
        self.assertEqual(price_str(79.99, None), "$79.99")

    def test_zero_list_price_is_treated_as_absent(self):
        self.assertEqual(price_str(79.99, 0), "$79.99")

    def test_with_list_price(self):
        self.assertEqual(price_str(79.99, 159.99), "$79.99 (was $159.99)")


class DiscountStrTests(unittest.TestCase):
    def test_percentage(self):
        self.assertEqual(discount_str(50), "50% off")

    def test_none_is_on_sale(self):
        self.assertEqual(discount_str(None), "On sale")

    def test_zero_is_on_sale(self):
        self.assertEqual(discount_str(0), "On sale")


class CompositionTests(unittest.TestCase):
    def test_bluesky_now_prefix_matches_historical_literal(self):
        # _build_bluesky_embed composes "Now " + price_str — must reproduce
        # the exact historical strings byte-for-byte.
        self.assertEqual(f"Now {price_str(12.34, 19.99)}", "Now $12.34 (was $19.99)")
        self.assertEqual(price_str(12.34, None), "$12.34")

    def test_weekly_digest_bullet_shape(self):
        price = price_str(59.99, 119.99)
        bullet = f"- [Woot] Samsung 990 Pro 2TB — {price} — 50.0% off"
        self.assertEqual(bullet, "- [Woot] Samsung 990 Pro 2TB — $59.99 (was $119.99) — 50.0% off")


if __name__ == "__main__":
    unittest.main()
