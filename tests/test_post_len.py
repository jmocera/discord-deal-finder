"""Tests for deal_bot.post_len — the shared "fit a post to the Bluesky
grapheme budget" logic. Stdlib only (unittest), same convention as the
other test files. Runnable via either:
    python -m unittest discover -s tests -p "test_*.py"
    pytest tests/
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot import post_len


class TruncateToTests(unittest.TestCase):
    def test_fits_without_ellipsis(self):
        self.assertEqual(post_len.truncate_to("short text", 300), "short text")

    def test_exact_fit_adds_no_ellipsis(self):
        self.assertEqual(post_len.truncate_to("x" * 298, 298), "x" * 298)

    def test_over_limit_appends_ellipsis(self):
        text = post_len.truncate_to("x" * 300, 298)
        self.assertLessEqual(len(text), 298)
        self.assertTrue(text.endswith("…"))

    def test_never_exceeds_limit(self):
        for n in range(200, 400):
            self.assertLessEqual(len(post_len.truncate_to("y" * n, 298)), 298)


class CaptionBudgetTests(unittest.TestCase):
    def test_budget_equals_297_minus_url_len(self):
        url = "https://www.amazon.com/dp/B08N5WRWNW?tag=voltdrop05-20"
        self.assertEqual(post_len.caption_budget(url), 297 - len(url))

    def test_budget_for_no_url_is_hard_target(self):
        self.assertEqual(post_len.caption_budget(None), post_len.HARD_TARGET)
        self.assertEqual(post_len.caption_budget(""), post_len.HARD_TARGET)


class SplitHashtagBlockTests(unittest.TestCase):
    def test_no_hashtags(self):
        self.assertEqual(post_len._split_hashtag_block("Good deal, no tags here."), ("Good deal, no tags here.", ""))

    def test_trailing_run_splits_off(self):
        prose, tags = post_len._split_hashtag_block("Real floor price. #PCBuild #SSDDeals")
        self.assertEqual(prose, "Real floor price.")
        self.assertEqual(tags, "#PCBuild #SSDDeals")

    def test_mid_sentence_tag_stays_in_prose(self):
        prose, tags = post_len._split_hashtag_block("This #SSD is great #Deals")
        self.assertEqual(prose, "This #SSD is great")
        self.assertEqual(tags, "#Deals")

    def test_all_hashtags(self):
        prose, tags = post_len._split_hashtag_block("#A #B #C")
        self.assertEqual(prose, "")
        self.assertEqual(tags, "#A #B #C")

    def test_blank_text(self):
        self.assertEqual(post_len._split_hashtag_block("   "), ("   ", ""))


class TrimProseTests(unittest.TestCase):
    def test_word_boundary_trim(self):
        self.assertEqual(post_len._trim_prose("one two three four", 10), "one two")

    def test_giant_single_token_hard_slices(self):
        self.assertEqual(post_len._trim_prose("A" * 500, 10), "A" * 10)

    def test_passthrough_when_enough_room(self):
        self.assertEqual(post_len._trim_prose("short", 300), "short")


class FitDealPostTests(unittest.TestCase):
    def test_happy_path_no_ellipsis(self):
        body = "Real floor price for this drive. #SSDDeals"
        url = "https://example.com/deal"
        out = post_len.fit_deal_post(body, url)
        self.assertEqual(out, body + "\n" + url)
        self.assertNotIn("…", out)

    def test_trims_prose_keeps_hashtags_and_url(self):
        url = "https://example.com/" + "x" * 160
        body = "A" * 300 + " #PCBuild #SSDDeals"
        out = post_len.fit_deal_post(body, url)
        self.assertLessEqual(len(out), post_len.HARD_TARGET)
        self.assertTrue(out.endswith(url))
        self.assertIn("#PCBuild #SSDDeals", out)
        self.assertTrue(out.startswith("A"))
        self.assertIn("…", out)

    def test_drops_hashtags_before_dropping_url(self):
        # URL leaves only ~1 code point of room even for prose-with-ellipsis,
        # so the fitter must drop the hashtags but keep the URL.
        url = "https://example.com/" + "y" * 274  # suffix = 296, one code point spare
        body = "A" * 300 + " #PCBuild #SSDDeals"
        out = post_len.fit_deal_post(body, url)
        self.assertLessEqual(len(out), post_len.HARD_TARGET)
        self.assertTrue(out.endswith(url))
        self.assertNotIn("#SSDDeals", out)  # tags dropped, URL kept

    def test_never_exceeds_target_in_any_case(self):
        url = "https://example.com/deal"
        for body in (
            "A" * 50,
            "A" * 300 + " #PCBuild #SSDDeals",
            "A" * 300 + " #PCBuild #SSDDeals #TechDeals #GamingMonitor",
            "A" * 500,
            "A" * 400 + " #OnlyTag",
            "#PCBuild #SSDDeals",
        ):
            self.assertLessEqual(len(post_len.fit_deal_post(body, url)), post_len.HARD_TARGET)

    def test_no_url_truncates_body(self):
        out = post_len.fit_deal_post("B" * 500, None)
        self.assertLessEqual(len(out), post_len.HARD_TARGET)

    def test_url_only_when_url_consumes_budget(self):
        # Degenerate: URL alone near the cap — must return the URL, not raise.
        url = "https://example.com/" + "z" * (post_len.HARD_TARGET - 3)
        self.assertEqual(post_len.fit_deal_post("A" * 400, url), url)

    def test_single_token_caption(self):
        url = "https://example.com/deal"
        out = post_len.fit_deal_post("A" * 500, url)
        self.assertLessEqual(len(out), post_len.HARD_TARGET)
        self.assertTrue(out.endswith(url))


class GraphemeLenTests(unittest.TestCase):
    def test_heart_emoji_counts_as_one_grapheme(self):
        s = "❤️" * 250  # each heart + VS16 is 2 code points, 1 grapheme
        self.assertEqual(post_len.grapheme_len(s), 250)
        self.assertEqual(len(s), 500)

    def test_invariant_grapheme_le_code_points(self):
        samples = ["héllo", "日本語", "👨‍👩‍👧", "🇺🇸 flag", "cafe\u0301", "a—b"]
        for s in samples:
            self.assertLessEqual(post_len.grapheme_len(s), len(s))


if __name__ == "__main__":
    unittest.main()