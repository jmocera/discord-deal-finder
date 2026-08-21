"""Weekly curated roundup written by AI from the week's posted deals.

Runs once a week (see .github/workflows/weekly_digest.yml), reads the
`posted_deals` Supabase table (an append-only log written by the pipeline's
`record_posted_deal`), and has the free Gemma model write a short roundup
which is posted to the digest Discord channel and to Bluesky.

One-time setup — run this in the Supabase SQL editor before the first run:

    create table if not exists posted_deals (
      id text primary key,
      source text,
      title text,
      url text,
      sale_price numeric,
      list_price numeric,
      posted_at timestamptz default now()
    );

Until that table exists, the pipeline's `record_posted_deal` fails silently
and this script's `fetch_recent_posted` returns an empty list, so nothing
breaks — the digest just has nothing to say.

CLI (mostly for testing the digest end-to-end safely):

    python -m deal_bot.weekly_digest               # normal run (posts)
    python -m deal_bot.weekly_digest --dry-run     # fetch + build, print, post nothing
    python -m deal_bot.weekly_digest --days 14     # widen the lookback window
    python -m deal_bot.weekly_digest --seed 7      # insert 7 fake rows (testing)
    python -m deal_bot.weekly_digest --clear       # delete every posted_deals row (testing cleanup)
    python -m deal_bot.weekly_digest --no-bluesky  # skip the Bluesky post (E2E testing)
"""

import argparse
from datetime import datetime, timedelta, timezone

import requests

from deal_bot import config
from deal_bot.ai.client import _call_openrouter
from deal_bot.integrations.bluesky import post_text_to_bluesky
from deal_bot.integrations.discord import build_weekly_digest_embed, _post_webhook
from deal_bot.storage.supabase import _supabase_headers

_PRUNE_DAYS = 90  # posted_deals older than this are deleted each run


def fetch_recent_posted(days: int = 7, limit: int | None = None) -> list[dict]:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    url = f"{config.SUPABASE_URL}/rest/v1/posted_deals"
    params = {"posted_at": f"gt.{cutoff}", "select": "id,source,title,url,sale_price,list_price"}
    if limit:
        params["limit"] = str(limit)
    try:
        resp = requests.get(url, headers=_supabase_headers(), params=params, timeout=15)
    except requests.RequestException as e:
        print(f"[weekly] posted_deals fetch failed: {e}")
        return []
    if resp.status_code != 200:
        print(f"[weekly] posted_deals fetch returned {resp.status_code}: {resp.text[:300]}")
        return []
    return resp.json()


def prune_posted_deals(ttl_days: int = _PRUNE_DAYS) -> None:
    """Delete posted_deals rows older than ttl_days so the table stays
    bounded (it's an append-only log, otherwise it grows forever)."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    url = f"{config.SUPABASE_URL}/rest/v1/posted_deals"
    try:
        resp = requests.delete(
            url, headers=_supabase_headers(), params={"posted_at": f"lt.{cutoff}"}, timeout=15
        )
    except requests.RequestException as e:
        print(f"[weekly] posted_deals prune failed: {e}")
        return
    if resp.status_code not in (200, 204):
        print(f"[weekly] posted_deals prune returned {resp.status_code}: {resp.text[:300]}")


def seed_posted_deals(count: int = 7) -> None:
    """Insert `count` fake rows for end-to-end testing. Delete them after
    with --clear (or manually). Only ever used by the operator for the E2E."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        print("[weekly] no Supabase config — cannot seed")
        return
    url = f"{config.SUPABASE_URL}/rest/v1/posted_deals"
    headers = _supabase_headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    names = [
        "Samsung 990 Pro 2TB NVMe SSD", "ASUS TUF Gaming RTX 4070 Ti",
        "Acer Nitro 27in 1440p 180Hz Monitor", "Crucial P3 Plus 1TB NVMe",
        "Logitech G502 X Wireless Mouse", "Elden Ring (Steam)", "Corsair RM850e PSU",
    ]
    rows = []
    for i in range(count):
        name = names[i % len(names)]
        sale, listed = 59.99 + i * 5, 119.99 + i * 10
        rows.append({
            "id": f"seed:{i}",
            "source": "Woot" if i % 2 else "Best Buy",
            "title": name,
            "url": f"https://example.com/seed/{i}",
            "sale_price": sale,
            "list_price": listed,
        })
    try:
        resp = requests.post(url, headers=headers, json=rows, timeout=15)
    except requests.RequestException as e:
        print(f"[weekly] seed failed: {e}")
        return
    if resp.status_code not in (200, 201, 204):
        print(f"[weekly] seed returned {resp.status_code}: {resp.text[:300]}")
        return
    print(f"[weekly] seeded {count} fake posted_deals rows (id prefix 'seed:')")


def clear_posted_deals() -> None:
    """Delete every posted_deals row (cleanup after a seed-based E2E)."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        print("[weekly] no Supabase config — nothing to clear")
        return
    url = f"{config.SUPABASE_URL}/rest/v1/posted_deals"
    try:
        resp = requests.delete(url, headers=_supabase_headers(), timeout=15)
    except requests.RequestException as e:
        print(f"[weekly] clear failed: {e}")
        return
    if resp.status_code not in (200, 204):
        print(f"[weekly] clear returned {resp.status_code}: {resp.text[:300]}")
        return
    print("[weekly] cleared all posted_deals rows")


def build_weekly_digest(deals: list[dict]) -> str:
    """One AI call (free Gemma → paid Gemma fallback) writing the roundup.
    Returns "" on total failure so the caller can skip posting."""
    if not deals or not config.OPENROUTER_API_KEY:
        return ""

    lines = []
    for d in deals:
        price = f"${d['sale_price']:.2f}"
        discount = ""
        if d.get("list_price"):
            price += f" (was ${d['list_price']:.2f})"
            pct = round((d["list_price"] - d["sale_price"]) / d["list_price"] * 100, 1)
            discount = f" — {pct}% off"
        lines.append(f"- [{d['source']}] {d['title']}{discount} — {price}")
    user_prompt = "\n".join(lines)

    for model in (config.OPENROUTER_WEEKLY_DIGEST_MODEL, config.OPENROUTER_WEEKLY_DIGEST_FALLBACK_MODEL):
        text = _call_openrouter(
            model, config.OPENROUTER_WEEKLY_DIGEST_SYSTEM_PROMPT, user_prompt,
            temperature=0.7, max_tokens=1200,
            # reasoning omitted: Gemma burns its token budget on reasoning
            # when any effort is set (see ai/deal_scorer.py).
        )
        if text:
            return text
    print("[weekly] digest model unavailable from both models this run")
    return ""


def run_weekly_digest(days: int = 7, limit: int | None = None, dry_run: bool = False, skip_bluesky: bool = False) -> bool:
    deals = fetch_recent_posted(days=days, limit=limit)
    if not deals:
        print("[weekly] no posted deals in window — skipping digest")
        return False

    text = build_weekly_digest(deals)
    if not text:
        return False

    if dry_run:
        print("[weekly] DRY RUN — not posting:")
        print("---")
        print(text)
        print("---")
        return True

    sent_discord = False
    if config.DIGEST_WEBHOOK_URL:
        sent_discord = _post_webhook(
            config.DIGEST_WEBHOOK_URL, {"embeds": [build_weekly_digest_embed(text)]}, "weekly-digest"
        )
        print(f"[weekly] discord posted={sent_discord}")
    else:
        print("[weekly] discord skipped (no DIGEST_WEBHOOK_URL)")

    if not skip_bluesky and config.BLUESKY_HANDLE and config.BLUESKY_APP_PASSWORD:
        posted = post_text_to_bluesky(text)
        print(f"[weekly] bluesky posted={posted}")
    else:
        print("[weekly] bluesky skipped (--no-bluesky or no credentials)")

    return sent_discord


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly AI deal roundup")
    parser.add_argument("--dry-run", action="store_true", help="fetch + build + print, post nothing")
    parser.add_argument("--days", type=int, default=7, help="lookback window in days")
    parser.add_argument("--limit", type=int, default=None, help="max rows to fetch")
    parser.add_argument("--seed", type=int, default=None, metavar="N", help="insert N fake rows (testing)")
    parser.add_argument("--clear", action="store_true", help="delete all posted_deals rows (testing cleanup)")
    parser.add_argument("--no-bluesky", action="store_true", help="skip the Bluesky post (E2E testing)")
    args = parser.parse_args()

    if args.seed is not None:
        seed_posted_deals(args.seed)
    elif args.clear:
        clear_posted_deals()
    else:
        # Normal run: prune old rows, then generate the digest.
        prune_posted_deals()
        run_weekly_digest(days=args.days, limit=args.limit, dry_run=args.dry_run, skip_bluesky=args.no_bluesky)


if __name__ == "__main__":
    main()