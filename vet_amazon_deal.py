#!/usr/bin/env python3
"""
vet_amazon_deal.py — standalone assistant for VoltDrop's manual Amazon
deal-posting workflow (see VoltDrop_Project_Scope.md §9). Amazon isn't a
data source in the automated deal_bot package (no scripted API access, no
affiliate program wired into the automated pipeline yet), so deals are
still found and posted by hand — this tool replaces the operator's manual
credibility checklist and copy-paste formatting with a repeatable one, it
does not post anything itself and is never invoked by the deal_bot package
or the GitHub Actions cron.

Takes a product URL, pasted page text, or a screenshot; extracts a clean
title, price, seller type, review count, and rating; runs the same
credibility checks the operator already applies by hand (seller type,
review count, rating, and whether the "discount" is real); and produces a
canonical affiliate link plus ready-to-copy Discord/Bluesky post text with
the required FTC `#ad` disclosure at the very front.

USAGE
-----
    python vet_amazon_deal.py --url "https://www.amazon.com/dp/B08N5WRWNW"
    python vet_amazon_deal.py --text "<pasted product page text>"
    python vet_amazon_deal.py --image "path/to/screenshot.png"
    python vet_amazon_deal.py                    # interactive prompts

--url also accepts pasted text directly (anything that doesn't look like
an http(s) URL is treated as --text) since a URL alone often isn't enough
to vet a listing — Amazon frequently blocks plain automated requests, in
which case this tool says so and asks for pasted text or a screenshot
instead, rather than attempting to evade that block.

Every extracted field is validated before use and falls back to None (not
guessed) on anything malformed — see _parse_vetting_json. The AI never
decides whether a deal passes; risk_assessment is a plain, deterministic
Python function over the validated fields (compute_risk_assessment).
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from deal_bot.ai.client import call_openrouter
from deal_bot.post_len import fit_deal_post, hard_target, truncate_to

load_dotenv(Path(__file__).resolve().parent / ".env")

# Report output uses a star glyph (see format_discord_copy) — on Windows,
# console stdout defaults to the legacy cp1252 codepage rather than UTF-8
# and raises UnicodeEncodeError on it. reconfigure() is a no-op failure
# (caught, not fatal) on anything where stdout isn't a real reconfigurable
# stream (e.g. redirected into certain non-standard pipes).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
# OPENROUTER_API_KEY is read by the shared client in deal_bot.ai.client (which
# pulls it from deal_bot.config) rather than being re-read locally.
OPENROUTER_AMAZON_TEXT_MODEL = os.environ.get("OPENROUTER_AMAZON_TEXT_MODEL", "google/gemini-2.5-flash-lite")
OPENROUTER_AMAZON_VISION_MODEL = os.environ.get("OPENROUTER_AMAZON_VISION_MODEL", "google/gemini-2.5-flash")

# Amazon Associates — approved and active (VoltDrop_Project_Scope.md §8).
AFFILIATE_TAG = "voltdrop05-20"

TEXT_SYSTEM_PROMPT = """You extract structured product-listing data from Amazon product page content (raw fetched page text or pasted text) for a deal-vetting tool. Only use information explicitly present in the input — never invent, estimate, or guess a value you don't actually see.

Extract:
- clean_title: the product name and model, stripped of SEO keyword clutter, under 150 characters. Null if you cannot determine one.
- sale_price: the current price the item can be bought for right now, as a plain number with no "$" or commas (e.g. 79.99). Null if not present.
- list_or_typical_price: the reference price the sale is discounted from, as a plain number. If the page shows Amazon's own "Typical price" (an algorithmic reference based on real sale history), prefer that over a seller-set "List Price," which can be inflated. Null if neither is present.
- seller_type: exactly one of "Sold/Shipped by Amazon", "Shipped by Amazon (3rd Party)", "3rd-Party Direct" — whichever matches what the listing states. Null if it's genuinely unclear.
- review_count: the total number of ratings/reviews shown, as a plain integer. Null if not shown.
- rating: the average star rating shown, as a number out of 5. Null if not shown.

Respond with only a JSON object in this exact shape: {"clean_title": string|null, "sale_price": number|null, "list_or_typical_price": number|null, "seller_type": string|null, "review_count": number|null, "rating": number|null}"""

VISION_SYSTEM_PROMPT = """You extract structured product-listing data from a screenshot of an Amazon product page for a deal-vetting tool. Only use information visibly present in the image — never invent, estimate, or guess a value you can't actually read.

Extract:
- clean_title: the product name and model, stripped of SEO keyword clutter, under 150 characters. Null if you cannot determine one.
- sale_price: the current price the item can be bought for right now, as a plain number with no "$" or commas (e.g. 79.99). Null if not visible.
- list_or_typical_price: the reference price the sale is discounted from, as a plain number. Prefer Amazon's own "Typical price" over a seller-set "List Price" if both are visible. Null if neither is visible.
- seller_type: exactly one of "Sold/Shipped by Amazon", "Shipped by Amazon (3rd Party)", "3rd-Party Direct" — whichever matches what's shown. Null if it's genuinely unclear from the screenshot.
- review_count: the total number of ratings/reviews shown, as a plain integer. Null if not visible.
- rating: the average star rating shown, as a number out of 5. Null if not visible.

Respond with only a JSON object in this exact shape: {"clean_title": string|null, "sale_price": number|null, "list_or_typical_price": number|null, "seller_type": string|null, "review_count": number|null, "rating": number|null}"""

_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # generous ceiling most vision APIs stay under

_EMPTY_FIELDS = {
    "clean_title": None, "sale_price": None, "list_or_typical_price": None,
    "seller_type": None, "review_count": None, "rating": None,
}
VALID_SELLER_TYPES = {"Sold/Shipped by Amazon", "Shipped by Amazon (3rd Party)", "3rd-Party Direct"}


# ---------------------------------------------------------------------------
# ASIN / URL CANONICALIZATION
# ---------------------------------------------------------------------------
_ASIN_PATH_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Za-z0-9]{10})(?=[/?]|$)")
_ASIN_QUERY_RE = re.compile(r"[?&]asin=([A-Za-z0-9]{10})", re.IGNORECASE)


def _asin_from_url_text(url: str) -> str | None:
    for pattern in (_ASIN_PATH_RE, _ASIN_QUERY_RE):
        m = pattern.search(url)
        if m:
            return m.group(1).upper()
    return None


def extract_asin(url: str) -> str | None:
    """Direct regex match first — covers the vast majority of amazon.com
    /.../dp/ASIN and gp/product/ASIN links with zero network calls. Only
    falls back to actually resolving redirects for shortened links
    (amzn.to, a.co) that don't carry the ASIN anywhere in the visible URL."""
    asin = _asin_from_url_text(url)
    if asin:
        return asin
    try:
        resp = requests.get(url, headers=_FETCH_HEADERS, allow_redirects=True, timeout=10, stream=True)
        resp.close()
    except requests.RequestException as e:
        print(f"[vet] couldn't resolve shortened URL: {e}")
        return None
    return _asin_from_url_text(resp.url)


def canonical_amazon_url(asin: str) -> str:
    """Rebuilt from scratch (ASIN + tag only) rather than editing the
    original URL — this is what actually guarantees ref=/qid=/sr=/every
    other tracking param is gone, instead of trying to enumerate and
    strip each one individually."""
    return f"https://www.amazon.com/dp/{asin}?tag={AFFILIATE_TAG}"


def looks_like_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s.strip(), re.IGNORECASE))


# ---------------------------------------------------------------------------
# PAGE FETCH (--url mode) — best-effort only, no bot-detection evasion
# ---------------------------------------------------------------------------
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _html_to_text(html: str, max_chars: int = 8000) -> str:
    """Crude tag-stripping, not a real HTML parser — good enough to hand
    an LLM surrounding page context, which is all this needs."""
    from html import unescape
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:max_chars]


def fetch_amazon_page_text(url: str) -> str | None:
    """Plain GET with a normal browser User-Agent — no proxies, no
    CAPTCHA-solving, no headless-browser fingerprint spoofing. Amazon
    frequently blocks or CAPTCHA-gates simple requests like this; when
    that happens this just returns None so the caller can fall back to
    asking for pasted text or a screenshot instead."""
    try:
        resp = requests.get(url, headers=_FETCH_HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"[vet] page fetch failed: {e}")
        return None
    if resp.status_code != 200:
        print(f"[vet] page fetch returned {resp.status_code} — Amazon may be blocking automated requests")
        return None
    text = _html_to_text(resp.text)
    if len(text) < 200:
        print("[vet] fetched page had very little extractable text — may have hit a CAPTCHA/bot-check page")
        return None
    return text


# ---------------------------------------------------------------------------
# IMAGE ENCODING (--image mode)
# ---------------------------------------------------------------------------
def _encode_image(path: Path) -> tuple[str, str] | None:
    try:
        data = path.read_bytes()
    except OSError as e:
        print(f"[vet] couldn't read image file {path}: {e}")
        return None
    if len(data) > _MAX_IMAGE_BYTES:
        print(f"[vet] image is {len(data) / 1_048_576:.1f}MB — over the {_MAX_IMAGE_BYTES // 1_048_576}MB limit, skipping")
        return None
    mime, _ = mimetypes.guess_type(str(path))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"  # reasonable default for a screenshot
    return mime, base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# OPENROUTER
# ---------------------------------------------------------------------------
# Shared client: `call_openrouter` is imported from deal_bot.ai.client (the
# single source of truth for the page URL, auth header, fail-open handling,
# and code-fence stripping). Reasoning is deliberately never set for this
# tool's models — the same "omit reasoning entirely" finding as
# deal_bot's spec extraction, confirmed empirically for
# google/gemini-2.5-flash-lite.

# ---------------------------------------------------------------------------
# EXTRACTION → VALIDATED FIELDS
# ---------------------------------------------------------------------------
def _parse_vetting_json(content: str | None) -> dict:
    """Strict per-field validation, no coercion — same posture as
    deal_bot's extract_clean_specs: a field that doesn't match the
    expected type/shape falls back to None rather than being guessed
    into something usable, since a wrong price or seller type here feeds
    directly into a credibility judgment."""
    if not content:
        return dict(_EMPTY_FIELDS)
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError) as e:
        print(f"[vet] model response didn't parse as JSON: {e}")
        return dict(_EMPTY_FIELDS)
    if not isinstance(parsed, dict):
        print(f"[vet] model response wasn't a JSON object: {parsed!r}")
        return dict(_EMPTY_FIELDS)

    fields = dict(_EMPTY_FIELDS)

    title = parsed.get("clean_title")
    if isinstance(title, str) and title.strip() and len(title.strip()) <= 150:
        fields["clean_title"] = title.strip()

    for key in ("sale_price", "list_or_typical_price"):
        value = parsed.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            fields[key] = float(value)

    seller_type = parsed.get("seller_type")
    if isinstance(seller_type, str) and seller_type in VALID_SELLER_TYPES:
        fields["seller_type"] = seller_type

    review_count = parsed.get("review_count")
    if isinstance(review_count, (int, float)) and not isinstance(review_count, bool) and review_count >= 0:
        fields["review_count"] = int(review_count)

    rating = parsed.get("rating")
    if isinstance(rating, (int, float)) and not isinstance(rating, bool) and 0 <= rating <= 5:
        fields["rating"] = float(rating)

    return fields


# ---------------------------------------------------------------------------
# RISK ASSESSMENT — deterministic Python over validated fields, never left
# to the model's judgment (mirrors the operator's existing manual
# checklist, VoltDrop_Project_Scope.md §9).
# ---------------------------------------------------------------------------
def compute_risk_assessment(fields: dict) -> dict:
    warnings: list[str] = []

    seller_type = fields.get("seller_type")
    if seller_type is None:
        warnings.append("Seller type could not be determined from the listing")
    elif seller_type != "Sold/Shipped by Amazon":
        warnings.append(f"Seller is '{seller_type}', not Sold/Shipped by Amazon")

    review_count = fields.get("review_count")
    if review_count is not None and review_count < 100:
        warnings.append(f"Low review count ({review_count})")

    rating = fields.get("rating")
    if rating is not None and rating < 4.0:
        warnings.append(f"Low rating ({rating})")

    sale_price = fields.get("sale_price")
    ref_price = fields.get("list_or_typical_price")
    if sale_price is not None and ref_price is not None and sale_price >= ref_price:
        warnings.append("Sale price is not actually below the reference price")

    passed = len(warnings) == 0
    verdict = "PASS — no credibility flags" if passed else "REVIEW — " + "; ".join(warnings)
    return {"passed": passed, "warnings": warnings, "verdict": verdict}


def _discount_pct(fields: dict) -> float | None:
    sale = fields.get("sale_price")
    ref = fields.get("list_or_typical_price")
    if sale is None or ref is None or ref <= 0 or sale >= ref:
        return None
    return round((ref - sale) / ref * 100, 1)


# ---------------------------------------------------------------------------
# READY-TO-COPY OUTPUT — #ad is always the literal first four characters,
# never appended, never conditional on a flag being set.
# ---------------------------------------------------------------------------
def format_discord_copy(vetted: dict) -> str:
    title = vetted.get("clean_title") or "Unknown product"
    lines = [f"#ad {title}"]

    price_line = f"${vetted['sale_price']:.2f}" if vetted.get("sale_price") is not None else "Price unavailable"
    if vetted.get("list_or_typical_price") is not None:
        price_line += f" (was ${vetted['list_or_typical_price']:.2f})"
    pct = _discount_pct(vetted)
    if pct is not None:
        price_line += f" — {pct}% off"
    lines.append(price_line)

    meta = []
    if vetted.get("rating") is not None:
        review_part = f" ({vetted['review_count']} ratings)" if vetted.get("review_count") is not None else ""
        meta.append(f"⭐ {vetted['rating']}{review_part}")
    if vetted.get("seller_type"):
        meta.append(vetted["seller_type"])
    if meta:
        lines.append(" | ".join(meta))

    if vetted.get("canonical_url"):
        lines.append(vetted["canonical_url"])
    return "\n".join(lines)


def format_bluesky_copy(vetted: dict) -> str:
    title = vetted.get("clean_title") or "Unknown product"
    price_bits = []
    if vetted.get("sale_price") is not None:
        price_bits.append(f"${vetted['sale_price']:.2f}")
        if vetted.get("list_or_typical_price") is not None:
            price_bits.append(f"(was ${vetted['list_or_typical_price']:.2f})")
    pct = _discount_pct(vetted)
    pct_str = f", {pct}% off" if pct is not None else ""
    price_str = " ".join(price_bits)
    body = f"#ad {title} — {price_str}{pct_str}".strip() if price_str else f"#ad {title}"

    url = vetted.get("canonical_url")
    if url:
        return fit_deal_post(body, url)
    return truncate_to(body, hard_target())


# ---------------------------------------------------------------------------
# VETTING ENTRY POINTS
# ---------------------------------------------------------------------------
def _finalize_vetting(fields: dict, source_url: str | None) -> dict:
    asin = extract_asin(source_url) if source_url else None
    canonical_url = canonical_amazon_url(asin) if asin else None
    risk = compute_risk_assessment(fields)
    return {**fields, "asin": asin, "canonical_url": canonical_url, "risk_assessment": risk}


def vet_from_text(raw_text: str, source_url: str | None = None) -> dict:
    content = call_openrouter(
        OPENROUTER_AMAZON_TEXT_MODEL, TEXT_SYSTEM_PROMPT, f"Page content:\n{raw_text[:8000]}",
        temperature=0.0, max_tokens=400, response_format={"type": "json_object"}, timeout=20,
    )
    fields = _parse_vetting_json(content)
    return _finalize_vetting(fields, source_url)


def vet_from_image(image_path: str, source_url: str | None = None) -> dict:
    encoded = _encode_image(Path(image_path))
    if encoded is None:
        fields = dict(_EMPTY_FIELDS)
    else:
        mime, b64 = encoded
        user_content = [
            {"type": "text", "text": "Screenshot of an Amazon product page:"},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
        content = call_openrouter(
            OPENROUTER_AMAZON_VISION_MODEL, VISION_SYSTEM_PROMPT, user_content,
            temperature=0.0, max_tokens=400, response_format={"type": "json_object"}, timeout=30,
        )
        fields = _parse_vetting_json(content)
    return _finalize_vetting(fields, source_url)


# ---------------------------------------------------------------------------
# REPORT / CLI
# ---------------------------------------------------------------------------
def print_report(vetted: dict) -> None:
    print("\n=== VoltDrop Amazon Deal Vetting Report ===")
    print(f"Title:          {vetted.get('clean_title') or '(unknown)'}")
    sale = vetted.get("sale_price")
    ref = vetted.get("list_or_typical_price")
    print(f"Sale price:     {'$%.2f' % sale if sale is not None else '(unknown)'}")
    print(f"Reference price:{(' $%.2f' % ref) if ref is not None else ' (unknown)'}")
    pct = _discount_pct(vetted)
    print(f"Discount:       {pct}% off" if pct is not None else "Discount:       (unknown)")
    print(f"Seller type:    {vetted.get('seller_type') or '(unknown)'}")
    print(f"Rating:         {vetted.get('rating') if vetted.get('rating') is not None else '(unknown)'}")
    print(f"Review count:   {vetted.get('review_count') if vetted.get('review_count') is not None else '(unknown)'}")
    print(f"ASIN:           {vetted.get('asin') or '(could not extract)'}")
    print(f"Affiliate link: {vetted.get('canonical_url') or '(no product URL/ASIN provided)'}")

    risk = vetted["risk_assessment"]
    print(f"\nCredibility check: {'PASSED' if risk['passed'] else 'NEEDS REVIEW'}")
    for w in risk["warnings"]:
        print(f"  - {w}")

    print("\n--- Discord copy ---")
    print(format_discord_copy(vetted))
    print("\n--- Bluesky copy ---")
    print(format_bluesky_copy(vetted))
    print()


def run_url_mode(url: str) -> None:
    url = url.strip()
    if not looks_like_url(url):
        # The CLI bundles URL and raw text under one --url flag ("URL/Text
        # mode") — anything that isn't itself a URL is treated as pasted
        # text rather than a hard error.
        run_text_mode(url, source_url=None)
        return

    page_text = fetch_amazon_page_text(url)
    if page_text is None:
        print("[vet] couldn't fetch the page directly (Amazon often blocks automated requests).")
        asin = extract_asin(url)
        if asin:
            print(f"Extracted ASIN {asin} anyway — affiliate link: {canonical_amazon_url(asin)}")
        print("Re-run with pasted page text (--text, or interactive mode) for full vetting.")
        return

    print_report(vet_from_text(page_text, source_url=url))


def run_text_mode(raw_text: str, source_url: str | None) -> None:
    raw_text = raw_text.strip()
    if not raw_text:
        print("[vet] no text provided.")
        return
    print_report(vet_from_text(raw_text, source_url=source_url))


def run_image_mode(image_path: str) -> None:
    path = Path(image_path.strip())
    if not path.is_file():
        print(f"[vet] no such file: {path}")
        return
    print_report(vet_from_image(str(path)))


def run_interactive() -> None:
    print("VoltDrop Amazon Deal Vetting Assistant")
    print("1) Amazon URL")
    print("2) Paste raw product page text")
    print("3) Screenshot file path")
    choice = input("Choose an input mode [1-3]: ").strip()

    if choice == "1":
        run_url_mode(input("Amazon URL: ").strip())
    elif choice == "2":
        print("Paste the product page text, then press Enter and Ctrl+D (Ctrl+Z then Enter on Windows) to finish:")
        run_text_mode(sys.stdin.read(), source_url=None)
    elif choice == "3":
        run_image_mode(input("Path to screenshot: ").strip())
    else:
        print("Not a valid choice.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="VoltDrop Amazon deal vetting assistant")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--url", help="Amazon product URL (or pasted text, if not a URL)")
    group.add_argument("--text", help="Raw pasted product page text")
    group.add_argument("--image", help="Path to a screenshot image")
    args = parser.parse_args()

    if args.url:
        run_url_mode(args.url)
    elif args.text:
        run_text_mode(args.text, source_url=None)
    elif args.image:
        run_image_mode(args.image)
    else:
        run_interactive()


if __name__ == "__main__":
    main()
