"""The orchestrator — fetch, filter, post, and log one full run."""

import argparse
import time
from datetime import datetime, timezone

from deal_bot import config, transport
from deal_bot.ai.categorizer import categorize_deals
from deal_bot.ai.classifier import classify_desirable_deals
from deal_bot.ai.deal_analyst import build_ai_analysis_batch
from deal_bot.ai.deal_scorer import score_deals
from deal_bot.ai.spec_extraction import extract_clean_specs_batch
from deal_bot.integrations.bluesky import post_to_bluesky
from deal_bot.integrations.discord import (
    build_categorizer_embed,
    build_digest_embed,
    build_quality_scorer_embed,
    build_run_log_embed,
    build_shadow_classification_embed,
    post_to_discord,
    _post_webhook,
)
from deal_bot.sources.bestbuy import fetch_bestbuy_search
from deal_bot.sources.steam import fetch_steam_specials
from deal_bot.sources.woot import fetch_woot_feed
from deal_bot.storage.price_history import (
    get_price_history_stats_bulk,
    record_price_observations,
)
from deal_bot.storage.supabase import (
    _supabase_headers,
    load_seen,
    prune_seen,
    record_posted_deal,
    upsert_seen_entry,
)


# ---------------------------------------------------------------------------
# RUN LOG — one row per run_once() call, written whether the run succeeds
# or raises, so run history is visible without needing to watch console
# output (important since this runs unattended on a GitHub Actions
# schedule with no console to check). Also mirrored to a Discord channel
# via RUN_LOG_WEBHOOK_URL, if set, so a failure is visible somewhere you'll
# actually notice rather than only being a queryable row in Supabase.
# ---------------------------------------------------------------------------
def log_run(
    *, deals_checked: int, posted: int, skipped_already_seen: int,
    skipped_no_better_price: int, skipped_below_threshold: int,
    skipped_not_near_historical_low: int, digest_sent: bool, error: str | None,
) -> None:
    if config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY:
        url = f"{config.SUPABASE_URL}/rest/v1/run_log"
        row = {
            "deals_checked": deals_checked,
            "posted": posted,
            "skipped_already_seen": skipped_already_seen,
            "skipped_no_better_price": skipped_no_better_price,
            "skipped_below_threshold": skipped_below_threshold,
            "skipped_not_near_historical_low": skipped_not_near_historical_low,
            "digest_sent": digest_sent,
            "error": error,
        }
        headers = _supabase_headers()
        headers["Prefer"] = "return=minimal"
        resp = transport.request("POST", url, headers=headers, json=[row])
        if resp is not None and resp.status_code not in (200, 201, 204):
            print(f"[supabase] run_log insert returned {resp.status_code}: {resp.text[:300]}")

    if config.RUN_LOG_WEBHOOK_URL:
        embed = build_run_log_embed(
            deals_checked=deals_checked, posted=posted,
            skipped_already_seen=skipped_already_seen,
            skipped_no_better_price=skipped_no_better_price,
            skipped_below_threshold=skipped_below_threshold,
            skipped_not_near_historical_low=skipped_not_near_historical_low,
            digest_sent=digest_sent, error=error,
        )
        _post_webhook(config.RUN_LOG_WEBHOOK_URL, {"embeds": [embed]}, "run-log")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def run_once() -> None:
    seen = load_seen()
    if seen is None:
        # A hard load failure means we can't trust dedupe state — running
        # with an empty seen map would treat every deal as new and risk
        # double-posting. Log and bail BEFORE fetching feeds so we don't
        # waste source calls on a run that will abort anyway.
        log_run(
            deals_checked=0, posted=0, skipped_already_seen=0,
            skipped_no_better_price=0, skipped_below_threshold=0,
            skipped_not_near_historical_low=0, digest_sent=False,
            error="load_seen failed (Supabase unreachable) — bailed to avoid double-posting",
        )
        return
    all_deals = []
    # Mutated in place by _process_deals rather than returned at the end —
    # if the loop raises partway through (after some deals already
    # posted), run_once() still has accurate partial counts for log_run()
    # instead of reporting an all-zero run that actually did something.
    stats = {
        "new_count": 0,
        "skipped_already_seen": 0,
        "skipped_no_better_price": 0,
        "skipped_below_threshold": 0,
        "skipped_not_near_historical_low": 0,
        "digest_sent": False,
    }
    # Tallied in-memory for the end-of-run digest — no need to persist
    # "source" into seen_deals since the digest only covers this single
    # run, not a longer window.
    digest_stats = {source: {"count": 0, "total_savings": 0.0, "best": None} for source in config.DIGEST_SOURCE_ORDER}

    # Everything below is wrapped so a run_log row gets written even if
    # something raises partway through — otherwise a crashed run would
    # leave no record at all, just a red X in the Actions tab.
    try:
        for feed in config.WOOT_FEEDS:
            all_deals.extend(fetch_woot_feed(feed))
            time.sleep(1.1)  # stay comfortably under Woot's 1 req/sec limit

        for term in config.BESTBUY_SEARCH_TERMS:
            all_deals.extend(fetch_bestbuy_search(term))
            time.sleep(0.3)

        all_deals.extend(fetch_steam_specials())  # single request, no loop needed

        # Woot's "Electronics" and "Computers" feeds can both list the
        # same item, and Best Buy's search terms can overlap the same way
        # (e.g. "gaming mouse" and "mouse" returning the same SKU) — so
        # all_deals can contain the same deal_id more than once before
        # this point. Dedup once, here, so every downstream step (price
        # history, the historical-low gate, and the posting loop) sees
        # each deal exactly once per run instead of double-counting it.
        all_deals = list({d["id"]: d for d in all_deals}.values())

        # Log a price observation for every fetched deal, not just ones
        # that end up posting — see the PRICE HISTORY section.
        record_price_observations(all_deals)

        # One batched lookup for every deal's price history instead of a
        # live request per deal inside the posting loop.
        history_map = get_price_history_stats_bulk([d["id"] for d in all_deals])

        _process_deals(all_deals, seen, digest_stats, stats, history_map)
    except Exception as e:
        log_run(
            deals_checked=len(all_deals), posted=stats["new_count"],
            skipped_already_seen=stats["skipped_already_seen"],
            skipped_no_better_price=stats["skipped_no_better_price"],
            skipped_below_threshold=stats["skipped_below_threshold"],
            skipped_not_near_historical_low=stats["skipped_not_near_historical_low"],
            digest_sent=stats["digest_sent"], error=str(e),
        )
        raise
    else:
        log_run(
            deals_checked=len(all_deals), posted=stats["new_count"],
            skipped_already_seen=stats["skipped_already_seen"],
            skipped_no_better_price=stats["skipped_no_better_price"],
            skipped_below_threshold=stats["skipped_below_threshold"],
            skipped_not_near_historical_low=stats["skipped_not_near_historical_low"],
            digest_sent=stats["digest_sent"], error=None,
        )


def _enrich_with_price_history(deal: dict, prior: dict | None) -> None:
    """Attach the price-history badge fields the embed/caption/analysis
    prompts read. "Lowest seen" here means the lowest we've ever ALERTED on
    (seen_deals), versus the item's true floor in price_history.

    is_new_low is STRICT (<): merely matching the previous low is not a new
    record. A tie takes the carry-forward branch, so the original
    lowest_price_date is preserved rather than refreshed to today."""
    prior_lowest = (prior or {}).get("lowest_price")
    sale = deal["sale_price"]
    deal["is_new_low"] = prior_lowest is not None and sale < prior_lowest
    if prior_lowest is not None and prior_lowest <= sale:
        deal["lowest_price"] = prior_lowest
        deal["lowest_price_date"] = prior.get("lowest_price_date")
    else:
        deal["lowest_price"] = sale
        deal["lowest_price_date"] = datetime.now(timezone.utc).isoformat()


def _skip_reason(deal: dict, prior: dict | None, history_days: int, history_low: float | None) -> str | None:
    """The deterministic pre-post filter, factored out so the gate decisions
    are independently testable. Returns the stats key explaining WHY the deal
    is skipped, or None when it should proceed to posting. Mirrors the
    historical behavior exactly."""
    if prior:
        prior_price = prior.get("sale_price")
        if prior_price is None:
            return "skipped_already_seen"
        if deal["sale_price"] >= prior_price - config.MIN_DOLLAR_SAVINGS:
            return "skipped_no_better_price"
    if deal["discount_pct"] is None or deal["discount_pct"] < config.MIN_DISCOUNT_PERCENT:
        return "skipped_below_threshold"
    if deal["list_price"] and (deal["list_price"] - deal["sale_price"]) < config.MIN_DOLLAR_SAVINGS:
        return "skipped_below_threshold"
    # Price-history quality gate: once there's enough real history for this
    # exact item, require the sale price to be near its own recorded floor,
    # not just far from the retailer's list price. Dormant until enough days.
    if history_days >= config.PRICE_HISTORY_MIN_DAYS and history_low is not None:
        ceiling = history_low * (1 + config.PRICE_HISTORY_TOLERANCE_PERCENT / 100)
        if deal["sale_price"] > ceiling:
            return "skipped_not_near_historical_low"
    return None


def _process_deals(
    all_deals: list[dict], seen: dict, digest_stats: dict, stats: dict, history_map: dict
) -> None:
    """Posting pipeline, structured in explicit phases so AI judgment runs
    BEFORE posting (enabling gate promotion later) while remaining
    behavior-neutral today — shadow features still don't gate anything.

    Phase A) deterministic filter -> candidates; B) batched AI enrichment
    (spec + analysis); C) post loop; D) capped Bluesky + digest + shadow
    reports. Mutates `stats` in place rather than returning at the end —
    run_once() depends on that for crash-accurate run_log counts."""
    bluesky_candidates = []  # collected here, ranked and capped after the loop
    posted_deals = []  # every deal that actually posted — for the shadow reports

    # ---- PHASE A — deterministic filter into a candidate list -------------
    candidates = []
    for deal in all_deals:
        prior = seen.get(deal["id"])
        history_days, history_low = history_map.get(deal["id"], (0, None))
        reason = _skip_reason(deal, prior, history_days, history_low)
        if reason is not None:
            stats[reason] += 1
            continue
        # Price-history tracking for the embed badge (see the helper's
        # docstring for the strict-new-low / tie-keeps-date semantics).
        _enrich_with_price_history(deal, prior)
        candidates.append(deal)

    # ---- PHASE B — batched AI enrichment ----------------------------------
    # Spec extraction, Woot/Best Buy only. Steam titles are already clean and
    # have no hardware specs to extract; forcing the schema on them would
    # either produce nothing or pressure the model into inventing specs.
    non_steam = [d for d in candidates if d["source"] != "Steam"]
    if non_steam:
        spec_results = extract_clean_specs_batch([d["title"] for d in non_steam])
        for deal, result in zip(non_steam, spec_results):
            deal["clean_title"] = result["clean_title"]
            deal["specs"] = result["specs"]
    for deal in candidates:
        if deal["source"] == "Steam":
            deal["clean_title"] = deal["title"]
            deal["specs"] = []

    # Analysis for every candidate (fails open to "" / no field).
    analyses = build_ai_analysis_batch(candidates)
    for deal, analysis in zip(candidates, analyses):
        deal["analysis"] = analysis

    # ---- PHASE C — post loop ---------------------------------------------
    for deal in candidates:
        if post_to_discord(deal):
            seen[deal["id"]] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sale_price": deal["sale_price"],
                "lowest_price": deal["lowest_price"],
                "lowest_price_date": deal["lowest_price_date"],
            }
            upsert_seen_entry(deal["id"], deal["source"], seen[deal["id"]])  # write immediately so a Ctrl+C or later failure doesn't lose this
            record_posted_deal(deal)  # append-only log backing the weekly digest (fails silent if the table isn't created yet)
            stats["new_count"] += 1
            posted_deals.append(deal)
            time.sleep(2)  # be gentle with the Discord webhook rate limit

            source_stats = digest_stats.setdefault(deal["source"], {"count": 0, "total_savings": 0.0, "best": None})
            source_stats["count"] += 1
            if deal["list_price"]:
                source_stats["total_savings"] += deal["list_price"] - deal["sale_price"]
            best = source_stats["best"]
            if best is None or (deal["discount_pct"] or 0) > (best["discount_pct"] or 0):
                source_stats["best"] = {
                    "title": deal["title"],
                    "url": deal["url"],
                    "discount_pct": deal["discount_pct"],
                }

            # Bluesky candidacy only — actual posting happens after the loop,
            # capped to the top BLUESKY_MAX_POSTS_PER_RUN by $ saved. Requires a
            # known list price to rank by savings.
            if (deal["discount_pct"] is not None and deal["discount_pct"] >= config.BLUESKY_MIN_DISCOUNT_PERCENT
                    and deal["list_price"]):
                bluesky_candidates.append(deal)

    # ---- PHASE: post-loop — prune, digest, capped bluesky -----------------
    prune_seen(config.SEEN_TTL_DAYS)
    print(
        f"[run] checked {len(all_deals)} deals — "
        f"{stats['new_count']} posted, "
        f"{stats['skipped_already_seen']} already posted at this price or better, "
        f"{stats['skipped_no_better_price']} same item but not enough of a price drop, "
        f"{stats['skipped_below_threshold']} below the discount/savings threshold, "
        f"{stats['skipped_not_near_historical_low']} not near their historical low"
    )

    # Only send a digest when there's something to report — an empty
    # "0 posted" message every run would just be noise.
    if stats["new_count"] > 0 and config.DIGEST_WEBHOOK_URL:
        stats["digest_sent"] = _post_webhook(
            config.DIGEST_WEBHOOK_URL, {"embeds": [build_digest_embed(digest_stats)]}, "digest"
        )

    bluesky_candidates.sort(key=lambda d: d["list_price"] - d["sale_price"], reverse=True)
    for deal in bluesky_candidates[:config.BLUESKY_MAX_POSTS_PER_RUN]:
        if post_to_bluesky(deal):
            print(f"[bluesky] posted: {deal['title'][:60]}")
        time.sleep(1)

    # SHADOW MODE: report what the desirability classifier would have
    # kept/dropped from this run's actual posts. Nothing here changes
    # what already posted above — this is purely for reviewing the
    # classifier's judgment before ever trusting it as a real gate.
    if posted_deals and config.SHADOW_CLASSIFIER_WEBHOOK_URL:
        keep, drop, model_used = classify_desirable_deals(posted_deals)
        if model_used:
            _post_webhook(
                config.SHADOW_CLASSIFIER_WEBHOOK_URL,
                {"embeds": [build_shadow_classification_embed(keep, drop, model_used)]},
                "shadow-classifier",
            )

    # SHADOW MODE: report the deal quality scorer's 1-10 ratings for the same
    # posts, and what it would have dropped below MIN_QUALITY_SCORE. Nothing
    # here changes what already posted — observation only, same promotion
    # discipline as the classifier above.
    if posted_deals and config.SHADOW_QUALITY_SCORER_WEBHOOK_URL:
        scores, model_used = score_deals(posted_deals)
        if model_used:
            _post_webhook(
                config.SHADOW_QUALITY_SCORER_WEBHOOK_URL,
                {"embeds": [build_quality_scorer_embed(posted_deals, scores, model_used, config.MIN_QUALITY_SCORE)]},
                "shadow-quality-scorer",
            )

    # SHADOW MODE: report the category tagger's per-deal classification.
    # Observation only — not yet used to gate or route posts.
    if posted_deals and config.SHADOW_CATEGORIZER_WEBHOOK_URL:
        categories, model_used = categorize_deals(posted_deals)
        if model_used:
            _post_webhook(
                config.SHADOW_CATEGORIZER_WEBHOOK_URL,
                {"embeds": [build_categorizer_embed(posted_deals, categories, model_used)]},
                "shadow-categorizer",
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="keep running instead of a single pass (local testing only — GitHub Actions uses its own schedule)")
    parser.add_argument("--interval", type=int, default=1800, help="seconds between polls when --loop is set")
    args = parser.parse_args()

    if args.loop:
        while True:
            run_once()
            time.sleep(args.interval)
    else:
        run_once()
