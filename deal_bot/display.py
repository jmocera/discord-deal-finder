"""Shared price/discount display strings.

Single source of truth for the "$X (was $Y)" / "N% off" fragments used
across captions, analysis prompts, embeds, and digest bullets, so the
formats can't drift apart between surfaces. Pure formatting — no I/O,
no config. (discord.build_embed intentionally does NOT use these: its
price line carries Discord markdown — **bold** / ~~strike~~ — which is
a different format by design.)
"""


def price_str(sale_price: float, list_price: float | None) -> str:
    """e.g. "$79.99" or "$79.99 (was $159.99)". A falsy list_price (None
    or 0) renders the sale price alone."""
    price = f"${sale_price:.2f}"
    if list_price:
        price += f" (was ${list_price:.2f})"
    return price


def discount_str(discount_pct: float | None) -> str:
    """e.g. "50% off", or "On sale" when there's no usable percentage
    (None or 0)."""
    if discount_pct:
        return f"{discount_pct}% off"
    return "On sale"
