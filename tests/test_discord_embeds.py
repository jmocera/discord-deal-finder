"""Tests for the shadow-mode Discord embed builders (deal_bot/integrations/
discord.py), specifically the partial-result rendering of the quality
scorer and categorizer reports.

Stdlib only (unittest). Runnable via either:
    python -m unittest discover -s tests -p "test_*.py"
    pytest tests/
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deal_bot.integrations.discord import (
    _join_capped,
    build_categorizer_embed,
    build_quality_scorer_embed,
)


def _deal(i: int) -> dict:
    return {
        "id": f"woot:test-{i}", "source": "Woot", "title": f"Deal {i}",
        "url": f"https://example.com/deal/{i}", "sale_price": 10.0 * i,
        "list_price": 20.0 * i, "discount_pct": 50.0,
    }


class JoinCappedTests(unittest.TestCase):
    def test_short_lines_all_included_no_suffix(self):
        lines = ["a", "bb", "ccc"]
        self.assertEqual(_join_capped(lines), "a\nbb\nccc")

    def test_empty_lines_give_empty_string(self):
        self.assertEqual(_join_capped([]), "")

    def test_overflow_drops_tail_and_appends_count(self):
        lines = [f"line-{i} " + "x" * 60 for i in range(30)]
        out = _join_capped(lines)
        self.assertLessEqual(len(out), 1024)
        self.assertTrue(out.startswith("line-0"))
        self.assertIn("…and", out)
        # The first dropped line's index is named in the notice.
        dropped = next(l for l in lines if l not in out)
        idx = int(dropped.split()[0].split("-")[1])
        self.assertIn(str(30 - idx), out)

    def test_never_exceeds_cap_for_any_input(self):
        for n in (1, 5, 13, 14, 15, 20, 40):
            lines = [f"[{'t' * 70}](https://example.com/{'u' * 100}) — Woot" for _ in range(n)]
            self.assertLessEqual(len(_join_capped(lines)), 1024)

    def test_single_giant_line_is_truncated_not_lost(self):
        giant = "y" * 5000
        out = _join_capped([giant])
        self.assertEqual(len(out), 1024)
        self.assertTrue(out.startswith("y"))


class EmbedOverflowTests(unittest.TestCase):
    """A big sale day (many posted deals) must not produce field values over
    Discord's 1024-char limit — that used to make the whole shadow webhook
    return 400 and silently lose the report."""

    def test_categorizer_embed_field_within_limit(self):
        deals = [_deal(i) for i in range(20)]
        categories = {d["id"]: "storage" for d in deals}
        embed = build_categorizer_embed(deals, categories, "m")
        scores_field = next(f for f in embed["fields"] if f["name"] == "Categories")
        self.assertLessEqual(len(scores_field["value"]), 1024)

    def test_quality_scorer_embed_field_within_limit(self):
        deals = [_deal(i) for i in range(20)]
        scores = {d["id"]: 7 for d in deals}
        embed = build_quality_scorer_embed(deals, scores, "m", 6)
        scores_field = next(f for f in embed["fields"] if f["name"] == "Scores")
        self.assertLessEqual(len(scores_field["value"]), 1024)

    def test_shadow_classification_long_urls_stay_within_limit(self):
        from deal_bot.integrations.discord import build_shadow_classification_embed
        drop = []
        for i in range(15):
            d = _deal(i)
            d["url"] = f"https://example.com/{'u' * 120}/{i}"  # long real-world URL
            d["title"] = "T" * 70
            drop.append(d)
        embed = build_shadow_classification_embed([], drop, "m")
        field = next(f for f in embed["fields"] if f["name"] == "Would have dropped")
        self.assertLessEqual(len(field["value"]), 1024)


class QualityScorerEmbedTests(unittest.TestCase):
    def test_missing_score_renders_question_mark(self):
        deals = [_deal(1), _deal(2), _deal(3)]
        scores = {"woot:test-1": 8, "woot:test-3": 4}  # test-2 unscored

        embed = build_quality_scorer_embed(deals, scores, "some-model", 6)

        scores_field = next(f for f in embed["fields"] if f["name"] == "Scores")
        lines = scores_field["value"].splitlines()
        self.assertIn("8/10", lines[0])
        self.assertIn("?/10", lines[1])
        self.assertIn("4/10", lines[2])

    def test_missing_score_is_never_counted_as_a_drop(self):
        deals = [_deal(1), _deal(2)]
        # test-2 would be < threshold IF scored, but it isn't — must NOT
        # be counted, and "Scored" must show 1/2 not 2/2.
        scores = {"woot:test-1": 9}

        embed = build_quality_scorer_embed(deals, scores, "some-model", 6)

        by_name = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertEqual(by_name["Scored"], "1/2")
        self.assertEqual(by_name["Would drop"], "0")

    def test_scored_below_threshold_counts_as_drop(self):
        deals = [_deal(1), _deal(2)]
        scores = {"woot:test-1": 3, "woot:test-2": 8}

        embed = build_quality_scorer_embed(deals, scores, "some-model", 6)

        by_name = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertEqual(by_name["Scored"], "2/2")
        self.assertEqual(by_name["Would drop"], "1")


class CategorizerEmbedTests(unittest.TestCase):
    def test_missing_category_renders_question_mark(self):
        deals = [_deal(1), _deal(2)]
        categories = {"woot:test-1": "storage"}  # test-2 uncategorized

        embed = build_categorizer_embed(deals, categories, "some-model")

        field = next(f for f in embed["fields"] if f["name"] == "Categories")
        lines = field["value"].splitlines()
        self.assertIn("`storage`", lines[0])
        self.assertIn("`?`", lines[1])

    def test_classified_header_shows_n_of_m(self):
        deals = [_deal(1), _deal(2)]
        categories = {"woot:test-1": "storage"}  # only 1 of 2 categorized

        embed = build_categorizer_embed(deals, categories, "some-model")

        by_name = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertEqual(by_name["Classified"], "1/2")

    def test_classified_header_shows_full_when_all_categorized(self):
        deals = [_deal(1), _deal(2)]
        categories = {"woot:test-1": "storage", "woot:test-2": "game"}

        embed = build_categorizer_embed(deals, categories, "some-model")

        by_name = {f["name"]: f["value"] for f in embed["fields"]}
        self.assertEqual(by_name["Classified"], "2/2")


if __name__ == "__main__":
    unittest.main()