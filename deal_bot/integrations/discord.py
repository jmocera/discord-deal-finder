"""Discord posting — embed builders and webhook delivery."""

import time
from datetime import datetime, timezone

import requests

from deal_bot import config
from deal_bot.ai.captions import build_ai_caption


def build_embed(deal: dict) -> dict:
    discount = deal["discount_pct"]
    # AI-cleaned title when available (see ai.spec_extraction), raw
    # title otherwise — never blocks on this being present.
    display_title = deal.get("clean_title") or deal["title"]

    # Color-code by deal quality, and flag standout deals in the title.
    if discount is not None and discount >= 50:
        color = 0xE74C3C  # red — hot deal
        title = f"🔥 {display_title[:245]}"
    elif discount is not None and discount >= 35:
        color = 0x2ECC71  # green — great deal
        title = f"✅ {display_title[:245]}"
    else:
        color = 0x3498DB  # blue — solid deal
        title = display_title[:250]

    price_value = f"**${deal['sale_price']:.2f}**"
    if deal["list_price"]:
        price_value += f"  ~~${deal['list_price']:.2f}~~"

    fields = [{"name": "Price", "value": price_value, "inline": True}]
    if discount is not None:
        fields.append({"name": "Discount", "value": f"{discount}% off", "inline": True})
    if deal["list_price"]:
        savings = deal["list_price"] - deal["sale_price"]
        fields.append({"name": "You Save", "value": f"${savings:.2f}", "inline": True})

    # Price-history context, based on prior posts of this exact deal ID
    # (see pipeline._process_deals) — only set when we have prior data.
    if deal.get("is_new_low"):
        fields.append({"name": "Price History", "value": "🏆 New record low!", "inline": True})
    elif deal.get("lowest_price") is not None and deal["lowest_price"] < deal["sale_price"]:
        low_date = (deal.get("lowest_price_date") or "")[:10] or "?"
        fields.append({
            "name": "Lowest Seen",
            "value": f"${deal['lowest_price']:.2f} ({low_date})",
            "inline": True,
        })

    # AI-extracted specs (Woot/Best Buy only — see ai.spec_extraction),
    # rendered as a bulleted block. Empty/absent whenever extraction
    # failed or genuinely found nothing worth calling out.
    if deal.get("specs"):
        fields.append({
            "name": "Specs",
            "value": "\n".join(f"• {s}" for s in deal["specs"]),
            "inline": False,
        })

    # AI analysis (see ai.deal_analyst) — an optional, longer "why this is
    # noteworthy" block. Absent whenever generation failed (fails open).
    if deal.get("analysis"):
        fields.append({
            "name": "Analysis",
            "value": deal["analysis"],
            "inline": False,
        })

    embed = {
        "title": title,
        "url": deal["url"],
        "color": color,
        "fields": fields,
        "footer": {"text": deal["source"]},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if deal.get("image"):
        key = "image" if config.EMBED_USE_LARGE_IMAGE else "thumbnail"
        embed[key] = {"url": deal["image"]}
    return embed


def build_digest_embed(stats: dict) -> dict:
    """stats: {source_name: {"count": int, "total_savings": float,
    "best": {"title": str, "url": str, "discount_pct": float} | None}}"""
    total_count = sum(s["count"] for s in stats.values())
    total_savings = sum(s["total_savings"] for s in stats.values())

    fields = []
    for source in config.DIGEST_SOURCE_ORDER:
        s = stats.get(source)
        if not s or s["count"] == 0:
            continue
        value = f"{s['count']} posted, ${s['total_savings']:.2f} saved"
        best = s.get("best")
        if best:
            discount_str = f" ({best['discount_pct']}% off)" if best["discount_pct"] is not None else ""
            value += f"\nBest: [{best['title'][:80]}]({best['url']}){discount_str}"
        fields.append({"name": source, "value": value, "inline": False})

    return {
        "title": "📊 Deal Digest",
        "description": f"{total_count} deals posted this run · ${total_savings:.2f} saved total",
        "color": 0x9B59B6,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_weekly_digest_embed(text: str) -> dict:
    """Embed for the weekly AI-written roundup (see weekly_digest.py)."""
    return {
        "title": "📊 Weekly Deal Roundup",
        "description": text[:4000],
        "color": 0x9B59B6,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_categorizer_embed(deals: list[dict], categories: dict[str, str], model_used: str) -> dict:
    """SHADOW MODE report — shows the category tagger's per-deal
    classification for this run's actual posts. Nothing was gated or routed."""
    fields = [
        {"name": "Classified", "value": str(len(deals)), "inline": True},
        {"name": "Model", "value": model_used, "inline": True},
    ]
    lines = [
        f"`{categories.get(d['id'], '?')}` — [{d['title'][:55]}]({d['url']})"
        for d in deals
    ]
    fields.append({"name": "Categories", "value": "\n".join(lines), "inline": False})

    return {
        "title": "🏷️ Category Tagger (Shadow Mode — nothing was actually routed)",
        "color": 0x95A5A6,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_run_log_embed(
    *, deals_checked: int, posted: int, skipped_already_seen: int,
    skipped_no_better_price: int, skipped_below_threshold: int,
    skipped_not_near_historical_low: int, digest_sent: bool, error: str | None,
) -> dict:
    if error:
        title, color = "❌ Run failed", 0xE74C3C
    else:
        title, color = "✅ Run completed", 0x2ECC71

    fields = [
        {"name": "Checked", "value": str(deals_checked), "inline": True},
        {"name": "Posted", "value": str(posted), "inline": True},
        {"name": "Digest sent", "value": "yes" if digest_sent else "no", "inline": True},
        {"name": "Already seen", "value": str(skipped_already_seen), "inline": True},
        {"name": "No better price", "value": str(skipped_no_better_price), "inline": True},
        {"name": "Below threshold", "value": str(skipped_below_threshold), "inline": True},
        {"name": "Not near historical low", "value": str(skipped_not_near_historical_low), "inline": True},
    ]
    if error:
        fields.append({"name": "Error", "value": error[:500], "inline": False})

    return {
        "title": title,
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_shadow_classification_embed(keep: list[dict], drop: list[dict], model_used: str) -> dict:
    """SHADOW MODE report — shows what the desirability classifier would
    have dropped from this run's actual posts, for review. Nothing was
    actually withheld; every deal in `keep` and `drop` combined posted
    normally this run."""
    fields = [
        {"name": "Kept", "value": str(len(keep)), "inline": True},
        {"name": "Would drop", "value": str(len(drop)), "inline": True},
        {"name": "Model", "value": model_used, "inline": True},
    ]
    if drop:
        lines = [f"[{d['title'][:70]}]({d['url']}) — {d['source']}" for d in drop[:10]]
        fields.append({"name": "Would have dropped", "value": "\n".join(lines), "inline": False})

    return {
        "title": "🔍 Desirability Classifier (Shadow Mode — nothing was actually withheld)",
        "color": 0x95A5A6,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_quality_scorer_embed(deals: list[dict], scores: dict[str, int], model_used: str, threshold: int) -> dict:
    """SHADOW MODE report — shows the deal quality scorer's 1-10 ratings for
    this run's actual posts, and which it would have dropped for scoring
    below `threshold`. Nothing was actually withheld."""
    would_drop = sum(1 for d in deals if scores.get(d["id"], 10) < threshold)

    fields = [
        {"name": "Scored", "value": str(len(deals)), "inline": True},
        {"name": "Would drop", "value": str(would_drop), "inline": True},
        {"name": "Threshold", "value": str(threshold), "inline": True},
        {"name": "Model", "value": model_used, "inline": True},
    ]
    lines = [
        f"{scores.get(d['id'], '?')}/10 — [{d['title'][:60]}]({d['url']})"
        for d in deals
    ]
    fields.append({"name": "Scores", "value": "\n".join(lines), "inline": False})

    return {
        "title": "🎯 Deal Quality Scorer (Shadow Mode — nothing was actually withheld)",
        "color": 0x95A5A6,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _post_webhook(webhook_url: str, payload: dict, label: str) -> bool:
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
        except requests.RequestException as e:
            print(f"[discord:{label}] post failed: {e}")
            return False

        if resp.status_code in (200, 204):
            return True

        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            wait = float(retry_after) + 0.25  # small buffer past what Discord asks for
            print(f"[discord:{label}] rate limited, waiting {wait:.2f}s (attempt {attempt}/{max_retries})")
            time.sleep(wait)
            continue

        print(f"[discord:{label}] webhook returned {resp.status_code}: {resp.text[:300]}")
        return False

    print(f"[discord:{label}] gave up after repeated rate limits")
    return False


def post_to_discord(deal: dict) -> bool:
    webhook_url = config.SOURCE_WEBHOOKS.get(deal["source"], "")
    if not webhook_url:
        print(f"[discord] no webhook URL set for {deal['source']} — skipping post")
        return False

    embed = build_embed(deal)
    success = _post_webhook(webhook_url, {"embeds": [embed]}, deal["source"].lower())

    # Best-effort mirror to the private review channel — its failure
    # shouldn't block the public post from counting as successful.
    if success and config.PRIVATE_WEBHOOK_URL:
        time.sleep(0.5)
        _post_webhook(config.PRIVATE_WEBHOOK_URL, {"embeds": [embed]}, "private")

        time.sleep(0.5)
        caption = build_ai_caption(deal)
        # X's real limit is 280 chars — flagged, not auto-trimmed, since
        # you're copying this by hand and can judge how best to cut it.
        warning = f"⚠️ {len(caption)} chars — over X's 280 limit, trim before posting\n" if len(caption) > 280 else ""
        # Code-block formatting gives you a one-click copy icon on hover
        # in the Discord client — no bot/buttons needed for that.
        _post_webhook(config.PRIVATE_WEBHOOK_URL, {"content": f"{warning}```{caption}```"}, "private-caption")

    return success