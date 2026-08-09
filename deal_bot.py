#!/usr/bin/env python3
"""
deal_bot.py — pulls electronics/PC-parts deals from Woot, Best Buy, and
Steam, filters out ones you've already posted or that aren't near their
own recorded price floor, and sends new ones to Discord via webhooks. Also
posts an end-of-run digest and auto-posts standout deals to Bluesky.

Designed to run as a single pass per invocation (no persistent process),
so it fits a GitHub Actions cron schedule or any other external scheduler
cleanly — see .github/workflows/deal_bot.yml.

STATE
-----
All state (seen-deal dedupe, price history, run log) lives in Supabase,
not on local disk — required for a scheduler like GitHub Actions, where
every run gets a fresh, empty filesystem. See SUPABASE_URL/SUPABASE_
SERVICE_KEY below. Tables expected (create these once via the Supabase
SQL editor):
  seen_deals    (id text pk, source text, last_seen timestamptz,
                 sale_price numeric, lowest_price numeric,
                 lowest_price_date timestamptz)
  price_history (id bigserial pk, deal_id text, source text,
                 observed_at timestamptz default now(), sale_price numeric,
                 list_price numeric, discount_pct numeric)
  run_log       (id bigserial pk, ran_at timestamptz default now(),
                 deals_checked int, posted int, skipped_already_seen int,
                 skipped_no_better_price int, skipped_below_threshold int,
                 skipped_not_near_historical_low int, digest_sent boolean,
                 error text)

SETUP
-----
1. pip install -r requirements.txt
2. Create a ".env" file in this folder (see .env.example for the full
   list of variables) with your real values. Locally, python-dotenv loads
   it automatically. In GitHub Actions, these are set as repo secrets
   instead and injected as real environment variables — .env is never
   committed (see .gitignore) and won't exist at all in that environment,
   which is fine: load_dotenv() silently does nothing if the file is missing.
3. Run it: python deal_bot.py
   - Always runs ONE pass and exits — intended for cron/GitHub Actions.
   - Pass --loop to keep running and poll every --interval seconds instead,
     for local testing without a scheduler.

NOTES
-----
- Best Buy's query syntax occasionally trips people up. If BESTBUY_SEARCH_
  TERMS calls start failing, the script prints the raw response so you can
  cross-check against https://bestbuyapis.github.io/api-documentation/
- Woot's rate limit is 1 req/sec, burst 10, 1000/day — this script's
  defaults stay well under that even polling every few minutes.
- The price-history quality gate (PRICE_HISTORY_MIN_DAYS/PERCENT below)
  stays dormant for an item until it has that many distinct days of
  recorded observations — expect it to have no effect for the first few
  days after this starts running regularly.
"""

import os
import time
import argparse
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests
from dotenv import load_dotenv

# Loads variables from a .env file (same folder as this script) into the
# environment. In GitHub Actions there is no .env file — the same names
# are injected as real environment variables from repo secrets instead,
# and this call is simply a no-op there.
load_dotenv(Path(__file__).resolve().parent / ".env")

# ---------------------------------------------------------------------------
# CONFIG — values come from the .env file locally, or repo secrets in
# GitHub Actions (see setup notes above)
# ---------------------------------------------------------------------------
WOOT_API_KEY = os.environ.get("WOOT_API_KEY", "")
BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

WOOT_WEBHOOK_URL = os.environ.get("WOOT_WEBHOOK_URL", "")
BESTBUY_WEBHOOK_URL = os.environ.get("BESTBUY_WEBHOOK_URL", "")
# Steam's public storefront "Specials" data — no API key needed, this is
# the same data that powers Steam's own front-page deals section.
STEAM_WEBHOOK_URL = os.environ.get("STEAM_WEBHOOK_URL", "")
# Optional: mirrors every deal that posts publicly into a private,
# owner-only channel — handy as a staging area for manually picking what
# to share elsewhere. Leave unset to skip this entirely.
PRIVATE_WEBHOOK_URL = os.environ.get("PRIVATE_WEBHOOK_URL", "")
# Dedicated channel for the end-of-run digest, separate from the per-deal
# source channels above.
DIGEST_WEBHOOK_URL = os.environ.get("DIGEST_WEBHOOK_URL", "")
# Dedicated channel that mirrors every run_log row (see log_run below) —
# posts every run, success or failure, so a crash is actually visible
# somewhere instead of only being a silent row in Supabase.
RUN_LOG_WEBHOOK_URL = os.environ.get("RUN_LOG_WEBHOOK_URL", "")

# Bluesky — free API, no approval process. Only standout deals auto-post
# here (see BLUESKY_MIN_DISCOUNT_PERCENT below), and even among those,
# only the top BLUESKY_MAX_POSTS_PER_RUN by $ saved actually go out — to
# avoid looking like a spam firehose on a brand-new account.
BLUESKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "")
BLUESKY_APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "")
BLUESKY_MIN_DISCOUNT_PERCENT = int(os.environ.get("BLUESKY_MIN_DISCOUNT_PERCENT", "50"))
BLUESKY_MAX_POSTS_PER_RUN = int(os.environ.get("BLUESKY_MAX_POSTS_PER_RUN", "2"))

# OpenRouter — AI-written captions for Bluesky and the private-channel
# copy-paste mirror, replacing the plain template. Tries the primary
# model, then the free fallback model, then the plain template as a last
# resort — this must never be able to block a post from going out.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_PRIMARY_MODEL = os.environ.get("OPENROUTER_PRIMARY_MODEL", "deepseek/deepseek-v4-flash-0731")
OPENROUTER_FALLBACK_MODEL = os.environ.get("OPENROUTER_FALLBACK_MODEL", "openai/gpt-oss-20b:free")
OPENROUTER_CAPTION_SYSTEM_PROMPT = """You write short, engaging social media captions for a deal-finding bot that posts real-time discounts on PC hardware, electronics, and PC games.

Output ONLY the caption text — no preamble, no explanation, no quotation marks, no markdown formatting, no code fences.

Keep the entire output under 200 characters. Mention the discount and price naturally, not as a bare stat dump. End with 2 to 4 relevant, space-separated hashtags chosen specifically for the item — vary them based on what the deal actually is. Never include a URL or link.

Example:
🔥 55% off this ASUS 27" gaming monitor — now $189.99, was $429.99. Big drop for a 165Hz panel. #PCBuild #GamingDeals #TechDeals"""

# SHADOW MODE: reports what the desirability classifier (see
# classify_desirable_deals) would have kept/dropped, without actually
# gating real posts on it yet — a dedicated channel to review its
# judgment against real deals before ever trusting it as a real filter.
SHADOW_CLASSIFIER_WEBHOOK_URL = os.environ.get("SHADOW_CLASSIFIER_WEBHOOK_URL", "")
OPENROUTER_CLASSIFIER_SYSTEM_PROMPT = """You screen deal listings for a bot that posts discounts to an audience of PC-building and PC-gaming enthusiasts. For each numbered item below, decide whether it is something that audience would genuinely want — not just topically related (e.g. "electronics"), but actually desirable: recognizable brands, real PC parts, monitors, peripherals, games, and similar. Reject generic, off-brand, or low-interest items even if they're topically in-category.

Respond with exactly one line per item, in the same order as the input, containing only the word KEEP or DROP — nothing else. No numbering, no explanation, no extra text. The number of output lines must exactly match the number of input items."""

# Woot feeds that map to your electronics/gaming focus.
# Valid options: All, Clearance, Computers, Electronics, Featured, Home,
# Gourmet, Shirts, Sports, Tools, Wootoff
WOOT_FEEDS = ["Electronics", "Computers"]

# Woot sometimes cross-lists a "featured" item across every feed regardless
# of category, so filtering by feed name alone isn't fully reliable. This
# catches anything with these words in the title and skips it. Add to this
# list as you spot more off-topic items sneaking through.
WOOT_EXCLUDE_KEYWORDS = [
    "squishmallow", "plush", "stuffed animal", "funko",
    "apparel", "shirt", "hoodie", "sneaker", "shoes",
    "cookware", "kitchen", "furniture", "decor", "bedding", "mattress",
]

# Allow-list for the PC-builds/monitors/gaming niche. A Woot deal must match
# at least one of these (in addition to clearing WOOT_EXCLUDE_KEYWORDS above)
# to post — this matters because Woot's "Electronics"/"Computers" feeds are
# broad categories that include plenty of on-category-but-off-brand items
# (e.g. printers, office gear) that the old exclude-only approach let through.
WOOT_INCLUDE_KEYWORDS = [
    "monitor", "display", "gpu", "graphics card", "video card",
    "motherboard", "cpu", "processor", "ram", "memory", "ssd", "nvme",
    "hard drive", "power supply", "psu", "pc case", "cpu cooler",
    "keyboard", "mouse", "mousepad", "headset", "webcam", "microphone",
    "laptop", "chromebook", "router", "gaming", "controller", "console",
]

# Woot's feed items carry a "Categories" field — a list of hierarchical
# strings like ["HOME", "TOOLS", "HOME/Lighting & Fans"]. This rejects
# whole off-topic departments by their top-level category (the part
# before the first "/"), as a coarser, less guessable complement to
# WOOT_EXCLUDE_KEYWORDS above — a new off-topic product line shows up here
# without needing its own keyword added first.
WOOT_EXCLUDE_CATEGORIES = [
    "HOME", "TOOLS", "APPAREL", "TOYS", "SPORTS", "KITCHEN",
    "AUTOMOTIVE", "GOURMET", "BEAUTY", "PET",
]

# Best Buy keyword searches — narrowed to match the PC-builds/monitors focus.
BESTBUY_SEARCH_TERMS = [
    "monitor", "graphics card", "motherboard", "power supply",
    "pc case", "ssd", "ram memory", "cpu cooler",
    "mechanical keyboard", "gaming mouse", "gaming headset",
    "webcam", "gaming console", "video game",
]

# Deal-quality thresholds — tunable via .env without editing code.
MIN_DISCOUNT_PERCENT = int(os.environ.get("MIN_DISCOUNT_PERCENT", "20"))      # ignore anything below this % off
MIN_DOLLAR_SAVINGS = float(os.environ.get("MIN_DOLLAR_SAVINGS", "10"))        # AND ignore anything saving less than this in real dollars
SEEN_TTL_DAYS = 45             # forget deals older than this so the table doesn't grow forever

# Price-history quality gate (see PRICE HISTORY section below). A deal
# needs at least this many DISTINCT CALENDAR DAYS of price_history
# observations before this gate applies at all — with no real history
# yet, everything falls back to the discount-vs-list-price check above.
PRICE_HISTORY_MIN_DAYS = int(os.environ.get("PRICE_HISTORY_MIN_DAYS", "3"))
# Once there's enough history, the sale price must be within this % of the
# lowest price ever recorded for that item to count as "near its floor."
PRICE_HISTORY_TOLERANCE_PERCENT = float(os.environ.get("PRICE_HISTORY_TOLERANCE_PERCENT", "5"))

# ---------------------------------------------------------------------------
# SEEN-DEAL TRACKING (dedupe across runs) — Supabase-backed.
# Table: seen_deals (id text pk, source text, last_seen timestamptz,
# sale_price numeric, lowest_price numeric, lowest_price_date timestamptz)
# ---------------------------------------------------------------------------
def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def load_seen() -> dict:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return {}
    url = f"{SUPABASE_URL}/rest/v1/seen_deals?select=id,source,last_seen,sale_price,lowest_price,lowest_price_date"
    try:
        # NOTE: PostgREST defaults to capping results (commonly 1000 rows)
        # with no pagination handled here — fine at today's volume with
        # SEEN_TTL_DAYS keeping the table bounded, but worth revisiting if
        # this table grows a lot.
        resp = requests.get(url, headers=_supabase_headers(), timeout=15)
    except requests.RequestException as e:
        print(f"[supabase] load failed: {e}")
        return {}

    if resp.status_code != 200:
        print(f"[supabase] load returned {resp.status_code}: {resp.text[:300]}")
        return {}

    seen = {}
    for row in resp.json():
        seen[row["id"]] = {
            "timestamp": row["last_seen"],
            "sale_price": row["sale_price"],
            "lowest_price": row["lowest_price"],
            "lowest_price_date": row["lowest_price_date"],
        }
    return seen


def upsert_seen_entry(deal_id: str, source: str, entry: dict) -> None:
    """Writes one row immediately after a successful post, so a Ctrl+C or
    later failure doesn't lose it — one row per post rather than
    rewriting a whole table/file each time."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/seen_deals"
    headers = _supabase_headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    row = {
        "id": deal_id,
        "source": source,
        "last_seen": entry["timestamp"],
        "sale_price": entry["sale_price"],
        "lowest_price": entry["lowest_price"],
        "lowest_price_date": entry["lowest_price_date"],
    }
    try:
        resp = requests.post(url, headers=headers, json=[row], timeout=15)
    except requests.RequestException as e:
        print(f"[supabase] upsert failed for {deal_id}: {e}")
        return
    if resp.status_code not in (200, 201, 204):
        print(f"[supabase] upsert for {deal_id} returned {resp.status_code}: {resp.text[:300]}")


def prune_seen(ttl_days: int) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    url = f"{SUPABASE_URL}/rest/v1/seen_deals"
    try:
        # Passed via params (not embedded in an f-string URL) so requests
        # percent-encodes the "+00:00" offset instead of it being read as
        # a literal space in the query string.
        resp = requests.delete(
            url, headers=_supabase_headers(), params={"last_seen": f"lt.{cutoff}"}, timeout=15
        )
    except requests.RequestException as e:
        print(f"[supabase] prune failed: {e}")
        return
    if resp.status_code not in (200, 204):
        print(f"[supabase] prune returned {resp.status_code}: {resp.text[:300]}")


# ---------------------------------------------------------------------------
# PRICE HISTORY — raw observations log, one row per fetched deal per run,
# whether or not it clears the posting threshold. Backs the quality gate
# in _process_deals (get_price_history_stats, below) and is also
# queryable directly in Supabase Studio for trends, e.g.
#   select min(sale_price), min(observed_at) from price_history
#   where deal_id = 'woot:...'
# ---------------------------------------------------------------------------
def record_price_observations(deals: list[dict]) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY or not deals:
        return
    url = f"{SUPABASE_URL}/rest/v1/price_history?on_conflict=deal_id,observed_date"
    today = date.today().isoformat()
    rows = [{
        "deal_id": d["id"],
        "source": d["source"],
        "sale_price": d["sale_price"],
        "list_price": d["list_price"],
        "discount_pct": d["discount_pct"],
        "observed_date": today,
    } for d in deals]
    headers = _supabase_headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    try:
        resp = requests.post(url, headers=headers, json=rows, timeout=30)
    except requests.RequestException as e:
        print(f"[supabase] price_history upsert failed: {e}")
        return
    if resp.status_code not in (200, 201, 204):
        print(f"[supabase] price_history upsert returned {resp.status_code}: {resp.text[:300]}")


def get_price_history_stats_bulk(deal_ids: list[str]) -> dict[str, tuple[int, float]]:
    """Batched replacement for querying price_history one deal at a time
    inside the posting loop — at 350+ deals a run (more once Best Buy is
    live), one live request per deal was 350+ sequential round-trips just
    for history lookups. This fetches everything in a handful of chunked
    requests instead. Returns {deal_id: (distinct_days_observed,
    lowest_price_ever_recorded)}; an ID with no history simply isn't a key
    in the result, so callers should use .get(id, (0, None)).

    Distinct days, not raw row count, on purpose — if this runs
    frequently, several observations can land on the same day without the
    retailer's price ever actually changing, which wouldn't tell us
    anything about real price behavior over time."""
    results: dict[str, tuple[int, float]] = {}
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY or not deal_ids:
        return results

    unique_ids = list(dict.fromkeys(deal_ids))  # de-dupe, keep it simple
    chunk_size = 100  # keeps each "in.(...)" query string comfortably short
    url = f"{SUPABASE_URL}/rest/v1/price_history"
    rows_by_deal: dict[str, list[tuple[str, float]]] = {}

    for i in range(0, len(unique_ids), chunk_size):
        chunk = unique_ids[i:i + chunk_size]
        try:
            resp = requests.get(
                url, headers=_supabase_headers(),
                params={"deal_id": f"in.({','.join(chunk)})", "select": "deal_id,sale_price,observed_at"},
                timeout=20,
            )
        except requests.RequestException as e:
            print(f"[supabase] price_history bulk stats failed: {e}")
            continue
        if resp.status_code != 200:
            print(f"[supabase] price_history bulk stats returned {resp.status_code}: {resp.text[:300]}")
            continue
        for row in resp.json():
            rows_by_deal.setdefault(row["deal_id"], []).append((row["observed_at"][:10], row["sale_price"]))

    for deal_id, rows in rows_by_deal.items():
        distinct_days = len({day for day, _ in rows})
        lowest_price = min(price for _, price in rows)
        results[deal_id] = (distinct_days, lowest_price)

    return results


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
    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        url = f"{SUPABASE_URL}/rest/v1/run_log"
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
        try:
            resp = requests.post(url, headers=headers, json=[row], timeout=15)
        except requests.RequestException as e:
            print(f"[supabase] run_log insert failed: {e}")
            resp = None
        if resp is not None and resp.status_code not in (200, 201, 204):
            print(f"[supabase] run_log insert returned {resp.status_code}: {resp.text[:300]}")

    if RUN_LOG_WEBHOOK_URL:
        embed = build_run_log_embed(
            deals_checked=deals_checked, posted=posted,
            skipped_already_seen=skipped_already_seen,
            skipped_no_better_price=skipped_no_better_price,
            skipped_below_threshold=skipped_below_threshold,
            skipped_not_near_historical_low=skipped_not_near_historical_low,
            digest_sent=digest_sent, error=error,
        )
        _post_webhook(RUN_LOG_WEBHOOK_URL, {"embeds": [embed]}, "run-log")


# ---------------------------------------------------------------------------
# WOOT
# ---------------------------------------------------------------------------
def fetch_woot_feed(feed_name: str) -> list[dict]:
    if not WOOT_API_KEY:
        return []
    url = f"https://developer.woot.com/feed/{feed_name}"
    headers = {"Accept": "application/json", "x-api-key": WOOT_API_KEY}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"[woot] request failed for feed '{feed_name}': {e}")
        return []

    if resp.status_code != 200:
        print(f"[woot] feed '{feed_name}' returned {resp.status_code}: {resp.text[:300]}")
        return []

    items = resp.json().get("Items", [])
    deals = []
    for item in items:
        if item.get("IsSoldOut"):
            continue
        title = item.get("Title", "")
        if any(kw in title.lower() for kw in WOOT_EXCLUDE_KEYWORDS):
            continue
        if not any(kw in title.lower() for kw in WOOT_INCLUDE_KEYWORDS):
            continue
        top_level_categories = {
            c.split("/")[0].strip().upper() for c in (item.get("Categories") or [])
        }
        if top_level_categories & {c.upper() for c in WOOT_EXCLUDE_CATEGORIES}:
            continue
        sale = (item.get("SalePrice") or {}).get("Minimum")
        list_price = (item.get("ListPrice") or {}).get("Minimum")
        if sale is None:
            continue
        discount_pct = None
        if list_price:
            discount_pct = round((list_price - sale) / list_price * 100, 1)
        deals.append({
            "id": f"woot:{item.get('OfferId')}",
            "source": "Woot",
            "title": title,
            "url": item.get("Url"),
            "image": item.get("Photo"),
            "list_price": list_price,
            "sale_price": sale,
            "discount_pct": discount_pct,
        })
    return deals


# ---------------------------------------------------------------------------
# BEST BUY
# ---------------------------------------------------------------------------
def fetch_bestbuy_search(term: str) -> list[dict]:
    if not BESTBUY_API_KEY:
        return []
    fields = "sku,name,salePrice,regularPrice,url,image,onSale"
    query = quote(f"search={term}&onSale=true")
    url = (
        f"https://api.bestbuy.com/v1/products({query})"
        f"?apiKey={BESTBUY_API_KEY}&format=json&show={fields}&pageSize=20"
    )
    try:
        resp = requests.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"[bestbuy] request failed for term '{term}': {e}")
        return []

    if resp.status_code != 200:
        print(f"[bestbuy] search '{term}' returned {resp.status_code}: {resp.text[:300]}")
        return []

    products = resp.json().get("products", [])
    deals = []
    for p in products:
        sale = p.get("salePrice")
        regular = p.get("regularPrice")
        if not sale or not regular or regular <= 0:
            continue
        discount_pct = round((regular - sale) / regular * 100, 1)
        deals.append({
            "id": f"bestbuy:{p.get('sku')}",
            "source": "Best Buy",
            "title": p.get("name"),
            "url": p.get("url"),
            "image": p.get("image"),
            "list_price": regular,
            "sale_price": sale,
            "discount_pct": discount_pct,
        })
    return deals


# ---------------------------------------------------------------------------
# STEAM
# ---------------------------------------------------------------------------
def fetch_steam_specials() -> list[dict]:
    """Pulls Steam's public "Specials" storefront category — this is the
    same public, no-key-required endpoint that powers Steam's own front
    page. It surfaces Valve's curated front-page picks rather than every
    discounted game on the platform, so expect a modest, curated list
    rather than a huge catalog dump."""
    url = "https://store.steampowered.com/api/featuredcategories?cc=us&l=en"
    try:
        resp = requests.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"[steam] request failed: {e}")
        return []

    if resp.status_code != 200:
        print(f"[steam] returned {resp.status_code}: {resp.text[:300]}")
        return []

    items = resp.json().get("specials", {}).get("items", [])
    deals = []
    for item in items:
        appid = item.get("id")
        final_cents = item.get("final_price")
        original_cents = item.get("original_price")
        if appid is None or final_cents is None:
            continue
        sale = final_cents / 100
        list_price = (original_cents / 100) if original_cents else None
        deals.append({
            "id": f"steam:{appid}",
            "source": "Steam",
            "title": item.get("name", "Unknown Game"),
            "url": f"https://store.steampowered.com/app/{appid}/",
            "image": item.get("large_capsule_image") or item.get("header_image"),
            "list_price": list_price,
            "sale_price": sale,
            "discount_pct": item.get("discount_percent"),
        })
    return deals


# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------
# Set True to show a large image instead of a small thumbnail — bigger and
# more eye-catching, but takes up more vertical space per post.
EMBED_USE_LARGE_IMAGE = False


def build_embed(deal: dict) -> dict:
    discount = deal["discount_pct"]

    # Color-code by deal quality, and flag standout deals in the title.
    if discount is not None and discount >= 50:
        color = 0xE74C3C  # red — hot deal
        title = f"🔥 {deal['title'][:245]}"
    elif discount is not None and discount >= 35:
        color = 0x2ECC71  # green — great deal
        title = f"✅ {deal['title'][:245]}"
    else:
        color = 0x3498DB  # blue — solid deal
        title = deal["title"][:250]

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
    # (see _process_deals) — only set when we have prior data.
    if deal.get("is_new_low"):
        fields.append({"name": "Price History", "value": "🏆 New record low!", "inline": True})
    elif deal.get("lowest_price") is not None and deal["lowest_price"] < deal["sale_price"]:
        low_date = (deal.get("lowest_price_date") or "")[:10] or "?"
        fields.append({
            "name": "Lowest Seen",
            "value": f"${deal['lowest_price']:.2f} ({low_date})",
            "inline": True,
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
        key = "image" if EMBED_USE_LARGE_IMAGE else "thumbnail"
        embed[key] = {"url": deal["image"]}
    return embed


# Fixed display order for the digest's per-source fields — sources with
# nothing posted this run are simply left out.
DIGEST_SOURCE_ORDER = ["Woot", "Best Buy", "Steam"]


def build_digest_embed(stats: dict) -> dict:
    """stats: {source_name: {"count": int, "total_savings": float,
    "best": {"title": str, "url": str, "discount_pct": float} | None}}"""
    total_count = sum(s["count"] for s in stats.values())
    total_savings = sum(s["total_savings"] for s in stats.values())

    fields = []
    for source in DIGEST_SOURCE_ORDER:
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
    """SHADOW MODE report — shows what classify_desirable_deals() would
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


def build_x_caption(deal: dict) -> str:
    """Plain-text, X-ready caption. No markdown — X doesn't render it.
    Trim manually before posting if it runs long; titles vary in length
    so this can't guarantee staying under X's character limit."""
    discount = f"{deal['discount_pct']}% off" if deal["discount_pct"] else "On sale"
    price = f"${deal['sale_price']:.2f}"
    if deal["list_price"]:
        price += f" (was ${deal['list_price']:.2f})"
    return f"{discount} — {deal['title']} — {price}\n{deal['url']}"


def _call_openrouter(
    model: str, system_prompt: str, user_prompt: str,
    *, temperature: float = 0.8, max_tokens: int = 350,
) -> str | None:
    if not OPENROUTER_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                # Both models here are reasoning models: part of the token
                # budget goes to an internal reasoning trace before the
                # actual answer. Without capping effort, a model can burn
                # the entire budget on it and leave content null —
                # reliably reproduced in testing with the free fallback
                # model at 0/5 successful calls before this was added.
                # "low" effort plus generous max_tokens (still fractions
                # of a cent either way) fixed that.
                "max_tokens": max_tokens,
                "reasoning": {"effort": "low"},
                "temperature": temperature,
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"[openrouter] request failed for model {model}: {e}")
        return None
    if resp.status_code != 200:
        print(f"[openrouter] model {model} returned {resp.status_code}: {resp.text[:300]}")
        return None
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError, TypeError) as e:
        print(f"[openrouter] unexpected response shape from {model}: {e}")
        return None
    if not content:
        # Present-but-null/empty content — e.g. a reasoning model that
        # burned its whole budget on the reasoning trace and never got to
        # write an answer. Not an exception, just "no usable output."
        print(f"[openrouter] {model} returned no usable content (reasoning may have exhausted the token budget)")
        return None
    # Small/free models occasionally ignore "no quotation marks" — strip a
    # single wrapping pair if present rather than rejecting the whole thing.
    content = content.strip().strip('"').strip("'").strip()
    return content or None


def build_ai_caption(deal: dict) -> str:
    """Tries OPENROUTER_PRIMARY_MODEL, then OPENROUTER_FALLBACK_MODEL, then
    the plain build_x_caption() template if both fail or OPENROUTER_API_KEY
    isn't set — this must never be able to block a post from going out.
    The URL is deliberately not part of the prompt; it's appended here in
    code so the LLM can't alter it and break the Bluesky link facet."""
    discount = f"{deal['discount_pct']}% off" if deal["discount_pct"] else "On sale"
    price = f"${deal['sale_price']:.2f}"
    if deal["list_price"]:
        price += f" (was ${deal['list_price']:.2f})"
    user_prompt = (
        f"Deal source: {deal['source']}\n"
        f"Item: {deal['title']}\n"
        f"Discount: {discount}\n"
        f"Price: {price}\n\n"
        "Write the caption."
    )

    for model in (OPENROUTER_PRIMARY_MODEL, OPENROUTER_FALLBACK_MODEL):
        caption = _call_openrouter(model, OPENROUTER_CAPTION_SYSTEM_PROMPT, user_prompt, temperature=0.8)
        if caption and len(caption) <= 260:  # sanity ceiling before trusting it
            return f"{caption}\n{deal['url']}"
        if caption:
            print(f"[openrouter] {model} response too long ({len(caption)} chars), trying next")

    return build_x_caption(deal)  # last resort: mechanical template


def classify_desirable_deals(deals: list[dict]) -> tuple[list[dict], list[dict], str | None]:
    """SHADOW MODE (not gating anything yet). One batched OpenRouter call
    judging whether each deal is genuinely desirable to a PC-building/
    gaming audience, beyond just having cleared the keyword/discount
    filters. Returns (keep, drop, model_used).

    Fails OPEN: if both models error or the response doesn't parse
    cleanly (wrong line count, anything other than KEEP/DROP), everything
    is kept. A wrong KEEP is a mediocre post; a wrong DROP would be an
    invisible lost deal — once/if this becomes a real gate, keeping
    everything is the safer failure direction. In shadow mode a failed
    call just means no report gets sent this run."""
    if not deals:
        return [], [], None
    if not OPENROUTER_API_KEY:
        return list(deals), [], None

    lines = [
        f"{i}. [{d['source']}] {d['title']} — {d['discount_pct']}% off, ${d['sale_price']:.2f}"
        for i, d in enumerate(deals, start=1)
    ]
    user_prompt = "\n".join(lines)
    # A generous floor, not just a per-item scale-up: reasoning overhead
    # doesn't shrink proportionally with a smaller item count, and a
    # too-tight budget reproduces the same null-content failure the
    # caption path hit before its fix — confirmed in testing (108 tokens
    # for a 6-item batch failed on both models).
    max_tokens = min(1500, 300 + len(deals) * 15)

    for model in (OPENROUTER_PRIMARY_MODEL, OPENROUTER_FALLBACK_MODEL):
        response = _call_openrouter(
            model, OPENROUTER_CLASSIFIER_SYSTEM_PROMPT, user_prompt,
            temperature=0.1, max_tokens=max_tokens,
        )
        if not response:
            continue
        verdicts = [line.strip().upper() for line in response.strip().splitlines() if line.strip()]
        if len(verdicts) != len(deals) or any(v not in ("KEEP", "DROP") for v in verdicts):
            print(
                f"[openrouter] classifier response from {model} didn't parse cleanly "
                f"({len(verdicts)} lines for {len(deals)} deals) — trying next"
            )
            continue
        keep = [d for d, v in zip(deals, verdicts) if v == "KEEP"]
        drop = [d for d, v in zip(deals, verdicts) if v == "DROP"]
        return keep, drop, model

    print("[openrouter] classifier unavailable from both models this run — no shadow report")
    return list(deals), [], None


SOURCE_WEBHOOKS = {
    "Woot": WOOT_WEBHOOK_URL,
    "Best Buy": BESTBUY_WEBHOOK_URL,
    "Steam": STEAM_WEBHOOK_URL,
}


def post_to_discord(deal: dict) -> bool:
    webhook_url = SOURCE_WEBHOOKS.get(deal["source"], "")
    if not webhook_url:
        print(f"[discord] no webhook URL set for {deal['source']} — skipping post")
        return False

    embed = build_embed(deal)
    success = _post_webhook(webhook_url, {"embeds": [embed]}, deal["source"].lower())

    # Best-effort mirror to the private review channel — its failure
    # shouldn't block the public post from counting as successful.
    if success and PRIVATE_WEBHOOK_URL:
        time.sleep(0.5)
        _post_webhook(PRIVATE_WEBHOOK_URL, {"embeds": [embed]}, "private")

        time.sleep(0.5)
        caption = build_ai_caption(deal)
        # X's real limit is 280 chars — flagged, not auto-trimmed, since
        # you're copying this by hand and can judge how best to cut it.
        warning = f"⚠️ {len(caption)} chars — over X's 280 limit, trim before posting\n" if len(caption) > 280 else ""
        # Code-block formatting gives you a one-click copy icon on hover
        # in the Discord client — no bot/buttons needed for that.
        _post_webhook(PRIVATE_WEBHOOK_URL, {"content": f"{warning}```{caption}```"}, "private-caption")

    return success


# ---------------------------------------------------------------------------
# BLUESKY
# ---------------------------------------------------------------------------
_bluesky_session = None  # cached for the duration of one run, avoids re-login per post


def _bluesky_login() -> dict | None:
    global _bluesky_session
    if _bluesky_session:
        return _bluesky_session
    if not BLUESKY_HANDLE or not BLUESKY_APP_PASSWORD:
        return None
    try:
        resp = requests.post(
            "https://bsky.social/xrpc/com.atproto.server.createSession",
            json={"identifier": BLUESKY_HANDLE, "password": BLUESKY_APP_PASSWORD},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[bluesky] login failed: {e}")
        return None
    _bluesky_session = resp.json()
    return _bluesky_session


def post_to_bluesky(deal: dict) -> bool:
    session = _bluesky_login()
    if not session:
        return False

    text = build_ai_caption(deal)  # AI-written when available, template on fallback
    if len(text) > 300:  # Bluesky's post length limit
        # Trim the caption body, not a blind tail-slice of the whole
        # string — the URL sits on the last line, and slicing the whole
        # thing could clip or remove it, silently breaking the link
        # facet below for anything long enough to need truncating.
        url_suffix = f"\n{deal['url']}"
        body = text[:-len(url_suffix)] if text.endswith(url_suffix) else text
        max_body_len = 300 - len(url_suffix) - 1  # -1 for the trailing "…"
        text = body[:max_body_len] + "…" + url_suffix

    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    # AT Protocol doesn't auto-linkify plain URLs in post text the way
    # most social apps do — without an explicit "facet" marking the byte
    # range and its target, a URL renders as inert plain text (exactly
    # what was happening). Byte offsets, not character offsets: facets
    # are defined over the UTF-8-encoded text, and this caption can
    # contain multi-byte characters (e.g. the em dash) before the URL.
    url_bytes = deal["url"].encode("utf-8")
    text_bytes = text.encode("utf-8")
    idx = text_bytes.find(url_bytes)
    if idx != -1:
        record["facets"] = [{
            "index": {"byteStart": idx, "byteEnd": idx + len(url_bytes)},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": deal["url"]}],
        }]

    try:
        resp = requests.post(
            "https://bsky.social/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[bluesky] post failed: {e}")
        return False

    if resp.status_code != 200:
        print(f"[bluesky] post returned {resp.status_code}: {resp.text[:300]}")
        return False
    return True


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def run_once() -> None:
    seen = load_seen()
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
    digest_stats = {source: {"count": 0, "total_savings": 0.0, "best": None} for source in DIGEST_SOURCE_ORDER}

    # Everything below is wrapped so a run_log row gets written even if
    # something raises partway through — otherwise a crashed run would
    # leave no record at all, just a red X in the Actions tab.
    try:
        for feed in WOOT_FEEDS:
            all_deals.extend(fetch_woot_feed(feed))
            time.sleep(1.1)  # stay comfortably under Woot's 1 req/sec limit

        for term in BESTBUY_SEARCH_TERMS:
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


def _process_deals(
    all_deals: list[dict], seen: dict, digest_stats: dict, stats: dict, history_map: dict
) -> None:
    """The main posting loop, split out so run_once() can wrap it in a
    single try/except for log_run without one giant indented block.
    Mutates `stats` (new_count, skipped_*, digest_sent) in place rather
    than returning at the end — see the comment in run_once()."""
    bluesky_candidates = []  # collected here, ranked and capped after the loop
    posted_deals = []  # every deal that actually posted this run — for the shadow classifier report

    for deal in all_deals:
        prior = seen.get(deal["id"])
        if prior:
            prior_price = prior.get("sale_price")
            if prior_price is None:
                stats["skipped_already_seen"] += 1
                continue
            if deal["sale_price"] >= prior_price - MIN_DOLLAR_SAVINGS:
                stats["skipped_no_better_price"] += 1
                continue
        if deal["discount_pct"] is None or deal["discount_pct"] < MIN_DISCOUNT_PERCENT:
            stats["skipped_below_threshold"] += 1
            continue
        if deal["list_price"] and (deal["list_price"] - deal["sale_price"]) < MIN_DOLLAR_SAVINGS:
            stats["skipped_below_threshold"] += 1
            continue

        # Price-history quality gate. Discount off the retailer's listed
        # price is a weak signal on its own (list prices get inflated) —
        # once there's enough real history for this exact item, require
        # the sale price to actually be near its own recorded floor, not
        # just far from a number the retailer picked. Dormant until
        # PRICE_HISTORY_MIN_DAYS of distinct-day history exists for a
        # given deal_id. Looked up from the batch fetched in run_once(),
        # not queried live — see get_price_history_stats_bulk().
        history_days, history_low = history_map.get(deal["id"], (0, None))
        if history_days >= PRICE_HISTORY_MIN_DAYS and history_low is not None:
            ceiling = history_low * (1 + PRICE_HISTORY_TOLERANCE_PERCENT / 100)
            if deal["sale_price"] > ceiling:
                stats["skipped_not_near_historical_low"] += 1
                continue

        # Price-history tracking for the embed badge. We only know about
        # prices from runs where this deal actually posted (below-
        # threshold prices are never recorded here), so "lowest seen"
        # means "lowest we've ever alerted on," not the item's true
        # all-time floor (that's what price_history above is for).
        prior_lowest = (prior or {}).get("lowest_price")
        is_new_low = prior_lowest is not None and deal["sale_price"] <= prior_lowest
        if prior_lowest is not None and prior_lowest < deal["sale_price"]:
            deal["lowest_price"] = prior_lowest
            deal["lowest_price_date"] = prior.get("lowest_price_date")
        else:
            deal["lowest_price"] = deal["sale_price"]
            deal["lowest_price_date"] = datetime.now(timezone.utc).isoformat()
        deal["is_new_low"] = is_new_low

        if post_to_discord(deal):
            seen[deal["id"]] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sale_price": deal["sale_price"],
                "lowest_price": deal["lowest_price"],
                "lowest_price_date": deal["lowest_price_date"],
            }
            upsert_seen_entry(deal["id"], deal["source"], seen[deal["id"]])  # write immediately so a Ctrl+C or later failure doesn't lose this
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

            # Bluesky candidacy only — actual posting happens after the
            # loop, capped to the top BLUESKY_MAX_POSTS_PER_RUN by $ saved.
            # Posting every qualifying deal immediately as it's found,
            # uncapped, is exactly the "spam firehose on a new account"
            # this threshold was meant to avoid. Requires a known list
            # price to rank by savings — the rare deal without one is
            # excluded from Bluesky consideration (still posts to Discord
            # as normal).
            if (deal["discount_pct"] is not None and deal["discount_pct"] >= BLUESKY_MIN_DISCOUNT_PERCENT
                    and deal["list_price"]):
                bluesky_candidates.append(deal)

    prune_seen(SEEN_TTL_DAYS)
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
    if stats["new_count"] > 0 and DIGEST_WEBHOOK_URL:
        stats["digest_sent"] = _post_webhook(
            DIGEST_WEBHOOK_URL, {"embeds": [build_digest_embed(digest_stats)]}, "digest"
        )

    bluesky_candidates.sort(key=lambda d: d["list_price"] - d["sale_price"], reverse=True)
    for deal in bluesky_candidates[:BLUESKY_MAX_POSTS_PER_RUN]:
        if post_to_bluesky(deal):
            print(f"[bluesky] posted: {deal['title'][:60]}")
        time.sleep(1)

    # SHADOW MODE: report what the desirability classifier would have
    # kept/dropped from this run's actual posts. Nothing here changes
    # what already posted above — this is purely for reviewing the
    # classifier's judgment before ever trusting it as a real gate.
    if posted_deals and SHADOW_CLASSIFIER_WEBHOOK_URL:
        keep, drop, model_used = classify_desirable_deals(posted_deals)
        if model_used:
            _post_webhook(
                SHADOW_CLASSIFIER_WEBHOOK_URL,
                {"embeds": [build_shadow_classification_embed(keep, drop, model_used)]},
                "shadow-classifier",
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


if __name__ == "__main__":
    main()
