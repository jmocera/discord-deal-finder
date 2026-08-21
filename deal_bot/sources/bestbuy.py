"""Best Buy deal source.

Best Buy's Products API is free but requires an API key that is still
pending approval as of the latest HANDOFF — so this source contributes
zero deals until `BESTBUY_API_KEY` is set. The query-encoding logic here
has not yet been exercised against a real key.
"""

import requests
from urllib.parse import quote

from deal_bot import config
from deal_bot.sources.base import discount_percent


def _redact(s: str) -> str:
    """Best Buy requires the API key in the request URL, so a connection
    failure's exception message can echo that URL (and the key) into
    logs/CI output. Strip it before printing."""
    if config.BESTBUY_API_KEY:
        s = s.replace(config.BESTBUY_API_KEY, "[REDACTED]")
    return s


def fetch_bestbuy_search(term: str) -> list[dict]:
    if not config.BESTBUY_API_KEY:
        return []
    fields = "sku,name,salePrice,regularPrice,url,image,onSale"
    query = quote(f"search={term}&onSale=true")
    url = (
        f"https://api.bestbuy.com/v1/products({query})"
        f"?apiKey={config.BESTBUY_API_KEY}&format=json&show={fields}&pageSize=20"
    )
    try:
        resp = requests.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"[bestbuy] request failed for term '{term}': {_redact(str(e))}")
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
        deals.append({
            "id": f"bestbuy:{p.get('sku')}",
            "source": "Best Buy",
            "title": p.get("name"),
            "url": p.get("url"),
            "image": p.get("image"),
            "list_price": regular,
            "sale_price": sale,
            "discount_pct": discount_percent(regular, sale),
        })
    return deals