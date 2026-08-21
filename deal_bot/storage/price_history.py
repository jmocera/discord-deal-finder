"""Price-history persistence — raw observations log, one row per fetched
deal per day, whether or not it clears the posting threshold. Backs the
quality gate in pipeline._process_deals and is also queryable directly in
Supabase Studio for trends.

Table: price_history (id bigserial pk, deal_id text, source text,
observed_at timestamptz default now(), sale_price numeric, list_price
numeric, discount_pct numeric, observed_date date) with a unique
constraint on (deal_id, observed_date), upserted via
`?on_conflict=deal_id,observed_date`.
"""

from datetime import date

import requests

from deal_bot import config
from deal_bot.storage.supabase import _supabase_headers


def record_price_observations(deals: list[dict]) -> None:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY or not deals:
        return
    url = f"{config.SUPABASE_URL}/rest/v1/price_history?on_conflict=deal_id,observed_date"
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
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY or not deal_ids:
        return results

    unique_ids = list(dict.fromkeys(deal_ids))  # de-dupe, keep it simple
    chunk_size = 100  # keeps each "in.(...)" query string comfortably short
    url = f"{config.SUPABASE_URL}/rest/v1/price_history"
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