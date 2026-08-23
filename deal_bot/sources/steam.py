"""Steam deal source.

Steam's public storefront "Specials" data — no API key needed, this is the
same data that powers Steam's own front-page deals section. Read-only GET,
so it goes through the shared transport (transient blips are retried
instead of losing the whole feed for this run).
"""

from deal_bot import transport


def fetch_steam_specials() -> list[dict]:
    """Pulls Steam's public "Specials" storefront category — this is the
    same public, no-key-required endpoint that powers Steam's own front
    page. It surfaces Valve's curated front-page picks rather than every
    discounted game on the platform, so expect a modest, curated list
    rather than a huge catalog dump."""
    url = "https://store.steampowered.com/api/featuredcategories?cc=us&l=en"
    resp = transport.request("GET", url, timeout=15)

    if resp is None:
        print("[steam] request failed after retries")
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