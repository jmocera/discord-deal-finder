"""Supabase seen-deal tracking — dedupe across runs.

Table: seen_deals (id text pk, source text, last_seen timestamptz,
sale_price numeric, lowest_price numeric, lowest_price_date timestamptz)

Accessed via the PostgREST REST API using `requests` — there is no SQL
execution tool here; any schema change is run by hand in the Supabase SQL
editor.
"""

from datetime import datetime, timedelta, timezone

import requests

from deal_bot import config


def _supabase_headers() -> dict:
    return {
        "apikey": config.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def load_seen() -> dict:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return {}
    url = f"{config.SUPABASE_URL}/rest/v1/seen_deals?select=id,source,last_seen,sale_price,lowest_price,lowest_price_date"
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
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return
    url = f"{config.SUPABASE_URL}/rest/v1/seen_deals"
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


def record_posted_deal(deal: dict) -> None:
    """Append-only log of every deal that actually posted, backing the
    weekly digest (weekly_digest.py). Separate from `seen_deals` (dedupe
    state) because this needs title/url, which seen_deals doesn't keep.

    Fails silent if the `posted_deals` table doesn't exist yet — see the
    CREATE TABLE statement in weekly_digest.py — so this can be wired into
    the pipeline before the table is created without breaking anything.

    Table: posted_deals (id text pk, source text, title text, url text,
    sale_price numeric, list_price numeric, posted_at timestamptz default
    now())"""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return
    url = f"{config.SUPABASE_URL}/rest/v1/posted_deals"
    headers = _supabase_headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    row = {
        "id": deal["id"],
        "source": deal["source"],
        "title": deal.get("clean_title") or deal["title"],
        "url": deal["url"],
        "sale_price": deal["sale_price"],
        "list_price": deal["list_price"],
    }
    try:
        resp = requests.post(url, headers=headers, json=[row], timeout=15)
    except requests.RequestException as e:
        print(f"[supabase] posted_deals insert failed for {deal['id']}: {e}")
        return
    if resp.status_code not in (200, 201, 204):
        print(f"[supabase] posted_deals insert for {deal['id']} returned {resp.status_code}: {resp.text[:300]}")


def prune_seen(ttl_days: int) -> None:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    url = f"{config.SUPABASE_URL}/rest/v1/seen_deals"
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