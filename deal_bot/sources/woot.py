"""Woot deal source.

Woot's API is free (developer.woot.com) and rate-limited to 1 req/sec,
burst 10, 1000/day — the pipeline sleeps 1.1s between feed requests to
stay comfortably under that. Read-only GET, so it goes through the shared
transport (transient blips are retried instead of losing the whole feed
for this run).
"""

from deal_bot import config, transport
from deal_bot.sources.base import discount_percent


def fetch_woot_feed(feed_name: str) -> list[dict]:
    if not config.WOOT_API_KEY:
        return []
    url = f"https://developer.woot.com/feed/{feed_name}"
    headers = {"Accept": "application/json", "x-api-key": config.WOOT_API_KEY}
    resp = transport.request("GET", url, headers=headers, timeout=15)

    if resp is None:
        print(f"[woot] feed '{feed_name}' failed after retries")
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
        if any(kw in title.lower() for kw in config.WOOT_EXCLUDE_KEYWORDS):
            continue
        if not any(kw in title.lower() for kw in config.WOOT_INCLUDE_KEYWORDS):
            continue
        top_level_categories = {
            c.split("/")[0].strip().upper() for c in (item.get("Categories") or [])
        }
        if top_level_categories & {c.upper() for c in config.WOOT_EXCLUDE_CATEGORIES}:
            continue
        sale = (item.get("SalePrice") or {}).get("Minimum")
        list_price = (item.get("ListPrice") or {}).get("Minimum")
        if sale is None:
            continue
        deals.append({
            "id": f"woot:{item.get('OfferId')}",
            "source": "Woot",
            "title": title,
            "url": item.get("Url"),
            "image": item.get("Photo"),
            "list_price": list_price,
            "sale_price": sale,
            "discount_pct": discount_percent(list_price, sale),
        })
    return deals