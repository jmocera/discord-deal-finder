"""Enhanced deal analysis for the Discord embed.

A longer, richer companion to the short caption "verdict" in captions.py —
same models, same fail-open discipline, but aimed at the Discord embed's
"Analysis" field rather than a 280-char social caption. Returns an empty
string on total failure, so it can never block a post or add a broken field.
"""

from deal_bot import config
from deal_bot.ai.client import _call_openrouter


def build_ai_analysis(deal: dict) -> str:
    """2-3 sentence expert analysis of *why* a deal is noteworthy, grounded
    in the same concrete signals as the caption (clean title, specs, and
    Supabase price-history context). Tries the primary model, then the free
    fallback, then returns "" (no analysis field) — never blocks a post.

    The URL is deliberately not included; analysis is rendered inside a
    Discord embed whose title already carries the link."""
    discount = f"{deal['discount_pct']}% off" if deal["discount_pct"] else "On sale"
    price = f"${deal['sale_price']:.2f}"
    if deal["list_price"]:
        price += f" (was ${deal['list_price']:.2f})"
    display_title = deal.get("clean_title") or deal["title"]
    specs = deal.get("specs") or []

    prompt_lines = [
        f"Deal source: {deal['source']}",
        f"Item: {display_title}",
    ]
    if specs:
        prompt_lines.append(f"Known specs: {', '.join(specs)}")
    prompt_lines.append(f"Discount: {discount}")
    prompt_lines.append(f"Price: {price}")
    if deal.get("is_new_low"):
        prompt_lines.append("Price history: this is a new all-time low for this exact item.")
    elif deal.get("lowest_price") is not None and deal["lowest_price"] < deal["sale_price"]:
        prompt_lines.append(f"Price history: the lowest ever tracked for this item was ${deal['lowest_price']:.2f}.")
    prompt_lines.append("")
    prompt_lines.append("Write the analysis.")
    user_prompt = "\n".join(prompt_lines)

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
