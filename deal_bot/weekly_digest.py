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
"""

from datetime import datetime, timedelta, timezone

import requests

from deal_bot import config
from deal_bot.ai.client import _call_openrouter
from deal_bot.integrations.bluesky import post_text_to_bluesky
from deal_bot.integrations.discord import build_weekly_digest_embed, _post_webhook
from deal_bot.storage.supabase import _supabase_headers


def fetch_recent_posted(days: int = 7) -> list[dict]:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    url = f"{config.SUPABASE_URL}/rest/v1/posted_deals"
    try:
        resp = requests.get(
            url, headers=_supabase_headers(),
            params={"posted_at": f"gt.{cutoff}", "select": "id,source,title,url,sale_price,list_price"},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[weekly] posted_deals fetch failed: {e}")
        return []
    if resp.status_code != 200:
        print(f"[weekly] posted_deals fetch returned {resp.status_code}: {resp.text[:300]}")
        return []
    return resp.json()


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


def run_weekly_digest() -> bool:
    deals = fetch_recent_posted()
    if not deals:
        print("[weekly] no posted deals in window — skipping digest")
        return False

    text = build_weekly_digest(deals)
    if not text:
        return False

    sent_discord = False
    if config.DIGEST_WEBHOOK_URL:
        sent_discord = _post_webhook(
            config.DIGEST_WEBHOOK_URL, {"embeds": [build_weekly_digest_embed(text)]}, "weekly-digest"
        )

    if config.BLUESKY_HANDLE and config.BLUESKY_APP_PASSWORD:
        post_text_to_bluesky(text)

    return sent_discord


def main() -> None:
    run_weekly_digest()


if __name__ == "__main__":
    main()