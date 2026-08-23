"""Enhanced deal analysis for the Discord embed.

A longer, richer companion to the short caption "verdict" in captions.py —
same models, same fail-open discipline, but aimed at the Discord embed's
"Analysis" field rather than a 280-char social caption. Returns an empty
string on total failure, so it can never block a post or add a broken field.
"""

import json
import re

from deal_bot import config
from deal_bot.ai.client import _call_openrouter
from deal_bot.display import discount_str, price_str

_BATCH_ANALYSIS_SYSTEM_PROMPT = """You write short expert analysis for a deal-finding bot aimed at PC builders and PC gamers. You'll be given a numbered list of deals, each with its source, item, known specs, discount, price, and optional price-history context. For each, write 2-3 concise sentences explaining what makes it genuinely noteworthy: what build or use case it fits, whether the price is strong for the specs given, and which spec(s) actually matter for that use case.

Take a direct, analytical, enthusiast tone. Do not use hype phrases like "insane deal" or "act now." Never state a spec, benchmark, or competitor price that wasn't explicitly given. Keep each item's analysis under 350 characters.

Respond with ONLY a JSON object in this exact shape, with EXACTLY one string per input item, in the same order:
{"items": ["analysis for item 1", "analysis for item 2", ...]}"""


def _format_deal_lines(deal: dict) -> list[str]:
    """The per-deal context lines shared by the individual and batched
    prompts (clean title, specs, price, price-history signal), so both
    versions feed the model the exact same data."""
    discount = discount_str(deal["discount_pct"])
    price = price_str(deal["sale_price"], deal["list_price"])
    display_title = deal.get("clean_title") or deal["title"]
    specs = deal.get("specs") or []

    lines = [
        f"Deal source: {deal['source']}",
        f"Item: {display_title}",
    ]
    if specs:
        lines.append(f"Known specs: {', '.join(specs)}")
    lines.append(f"Discount: {discount}")
    lines.append(f"Price: {price}")
    if deal.get("is_new_low"):
        lines.append("Price history: this is a new all-time low for this exact item.")
    elif deal.get("lowest_price") is not None and deal["lowest_price"] < deal["sale_price"]:
        lines.append(f"Price history: the lowest ever tracked for this item was ${deal['lowest_price']:.2f}.")
    return lines


def _parse_items(content: str | None) -> list[str] | None:
    """Turn model output into a list of analysis strings, or None."""
    if not content:
        return None
    parsed = None
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        pass
    if not isinstance(parsed, dict):
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except (ValueError, TypeError):
                parsed = None
    if not isinstance(parsed, dict):
        return None
    items = parsed.get("items")
    if not (isinstance(items, list) and all(isinstance(s, str) for s in items)):
        return None
    return [s for s in items]


def build_ai_analysis(deal: dict) -> str:
    """2-3 sentence expert analysis of *why* a deal is noteworthy, grounded
    in the same concrete signals as the caption (clean title, specs, and
    Supabase price-history context). Tries the primary model, then the free
    fallback, then returns "" (no analysis field) — never blocks a post.

    The URL is deliberately not included; analysis is rendered inside a
    Discord embed whose title already carries the link."""
    user_prompt = "\n".join(_format_deal_lines(deal) + ["", "Write the analysis."])

    for model in (config.OPENROUTER_PRIMARY_MODEL, config.OPENROUTER_FALLBACK_MODEL):
        analysis = _call_openrouter(
            model, config.OPENROUTER_ANALYSIS_SYSTEM_PROMPT, user_prompt,
            temperature=0.4, reasoning={"effort": "low"}, max_tokens=700,
        )
        if analysis and len(analysis) <= 380:
            return analysis
        if analysis:
            print(f"[openrouter] {model} analysis failed validation (len={len(analysis)}), trying next")

    return ""


def build_ai_analysis_batch(deals: list[dict]) -> list[str]:
    """Batched version of build_ai_analysis — one call for N deals instead of
    N sequential calls. Same models/context per deal. If the batch doesn't
    parse to N valid analyses, falls back to the per-item function so a
    degraded batch never produces worse output than the previous behavior."""
    if not deals:
        return []
    if not config.OPENROUTER_API_KEY:
        return ["" for _ in deals]

    lines = []
    for i, deal in enumerate(deals, start=1):
        lines.append(f"{i}. " + " | ".join(_format_deal_lines(deal)))
    user_prompt = "\n".join(lines)

    max_tokens = min(8000, 400 + len(deals) * 130)
    items = None
    for model in (config.OPENROUTER_PRIMARY_MODEL, config.OPENROUTER_FALLBACK_MODEL):
        content = _call_openrouter(
            model, _BATCH_ANALYSIS_SYSTEM_PROMPT, user_prompt,
            temperature=0.4, reasoning={"effort": "low"}, max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        items = _parse_items(content)
        if items is not None and len(items) == len(deals) and all(0 < len(s) <= 380 for s in items):
            return items
        print(f"[openrouter] batch analysis from {model} unusable — trying next")

    # The batch came back unusable — fall back to the per-item function so
    # each deal still gets its own dedicated analysis attempt.
    print("[openrouter] batch analysis unusable — falling back to per-item")
    return [build_ai_analysis(d) for d in deals]