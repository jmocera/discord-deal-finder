"""Supabase seen-deal tracking — dedupe across runs.

Table: seen_deals (id text pk, source text, last_seen timestamptz,
sale_price numeric, lowest_price numeric, lowest_price_date timestamptz)

Accessed via the PostgREST REST API using `requests` — there is no SQL
execution tool here; any schema change is run by hand in the Supabase SQL
editor.
"""

from datetime import datetime, timedelta, timezone

from deal_bot import config, transport


def _supabase_headers() -> dict:
    return {
        "apikey": config.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def load_seen() -> dict | None:
    """Load seen-deal dedupe state. Returns a dict on success, None on a hard
    fetch failure (network exhausted or non-200), and {} only when there's no
    Supabase config (skip). Callers treat None as fatal — running on an empty
    seen map would treat every deal as new and risk double-posting."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return {}
    url = f"{config.SUPABASE_URL}/rest/v1/seen_deals?select=id,source,last_seen,sale_price,lowest_price,lowest_price_date"
    resp = transport.request("GET", url, headers=_supabase_headers())
    if resp is None:
        print("[supabase] load failed after retries")
        return None
    if resp.status_code != 200:
        print(f"[supabase] load returned {resp.status_code}: {resp.text[:300]}")
        return None

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
    resp = transport.request("POST", url, headers=headers, json=[row])
    if resp is None:
        print(f"[supabase] upsert failed for {deal_id} after retries")
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
        # Explicit timestamp, not the column default: merge-duplicates
        # overwrites every supplied column on conflict, and seen_deals
        # TTL-prunes after SEEN_TTL_DAYS — so a re-post of the same deal ID
        # must refresh posted_at or the weekly digest's time window would
        # keep seeing only the original post date.
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = transport.request("POST", url, headers=headers, json=[row])
    if resp is None:
        print(f"[supabase] posted_deals insert failed for {deal['id']} after retries")
        return
    if resp.status_code not in (200, 201, 204):
        print(f"[supabase] posted_deals insert for {deal['id']} returned {resp.status_code}: {resp.text[:300]}")


def prune_seen(ttl_days: int) -> None:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    url = f"{config.SUPABASE_URL}/rest/v1/seen_deals"
    resp = transport.request("DELETE", url, headers=_supabase_headers(), params={"last_seen": f"lt.{cutoff}"})
    if resp is None:
        print("[supabase] prune failed after retries")
        return
    if resp.status_code not in (200, 204):
        print(f"[supabase] prune returned {resp.status_code}: {resp.text[:300]}")