"""Shared deal contract and fetch interface for all sources.

Every source returns a list of dicts with this shape (loosely a TypedDict;
kept as plain dicts to avoid runtime import overhead and because sources
build them directly):

    {
        "id": str,            # "source:<sku/offerid/appid>"
        "source": str,        # "Woot" | "Best Buy" | "Steam"
        "title": str,
        "url": str,
        "image": str | None,
        "list_price": float | None,
        "sale_price": float,
        "discount_pct": float | None,
    }
"""

from typing import TypedDict


class Deal(TypedDict, total=False):
    """The on-the-wire deal shape every source produces and the pipeline
    consumes. Extra keys (``clean_title``, ``specs``, ``lowest_price``,
    ``is_new_low``, ...) are attached downstream by the pipeline/AI steps."""
    id: str
    source: str
    title: str
    url: str
    image: str | None
    list_price: float | None
    sale_price: float
    discount_pct: float | None


def discount_percent(list_price: float | None, sale_price: float) -> float | None:
    """Shared discount-of-list-price calculation; None when there's no list
    price to compute it against."""
    if not list_price or list_price <= 0:
        return None
    return round((list_price - sale_price) / list_price * 100, 1)