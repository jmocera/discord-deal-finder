"""AI caption "verdicts" for Bluesky and the private-channel mirror."""

import re

from deal_bot import config
from deal_bot.ai.client import _call_openrouter
from deal_bot.display import discount_str, price_str
from deal_bot.post_len import caption_budget

_HASHTAG_PATTERN = re.compile(r"#(\w+)")


def build_x_caption(deal: dict) -> str:
    """Plain-text, X-ready caption. No markdown — X doesn't render it.
    Trim manually before posting if it runs long; titles vary in length
    so this can't guarantee staying under X's character limit."""
    return build_x_caption_body(deal) + "\n" + deal["url"]


def build_x_caption_body(deal: dict) -> str:
    """Mechanical template body (no URL appended) — the same discount/price
    line build_x_caption() has always produced, split out so the Bluesky
    fitter can budget the URL separately."""
    discount = discount_str(deal["discount_pct"])
    price = price_str(deal["sale_price"], deal["list_price"])
    display_title = deal.get("clean_title") or deal["title"]
    return f"{discount} — {display_title} — {price}"


def _hashtags_look_reasonable(text: str) -> bool:
    """Light sanity check, not a hard allow-list — the model is trusted
    to pick contextually relevant hashtags per item (deliberately: real
    output like #SSDDeals, #BaldursGate3, #GamingMonitor is more useful
    than a fixed generic vocabulary would be), this just catches
    obviously broken or spammy output before it posts. Also rejects any
    URL the model sneaks in — the link is appended in code, and a
    model-injected URL would consume budget and mis-target the link
    facet."""
    if re.search(r"https?://\S+", text):
        return False
    tags = _HASHTAG_PATTERN.findall(text)
    return len(tags) <= 4 and all(1 <= len(tag) <= 30 for tag in tags)


def build_ai_caption(deal: dict) -> str:
    """Caption body plus the URL line — the public contract used by the
    private-channel mirror and callers that want the URL appended."""
    return build_ai_caption_body(deal) + "\n" + deal["url"]


def build_ai_caption_body(deal: dict) -> str:
    """Tries OPENROUTER_PRIMARY_MODEL, then OPENROUTER_FALLBACK_MODEL, then
    the plain build_x_caption_body() template if both fail or
    OPENROUTER_API_KEY isn't set — this must never be able to block a post
    from going out. Returns the caption WITHOUT the URL; the caller appends
    it (via fit_deal_post) so the LLM can't alter it and break the Bluesky
    link facet.

    Feeds the model concrete, already-verified signals (clean title,
    specs, and price-history context from Supabase) so it acts as an
    analytical synthesizer of real data rather than an ungrounded
    copywriter — see config.OPENROUTER_CAPTION_SYSTEM_PROMPT."""
    discount = discount_str(deal["discount_pct"])
    price = price_str(deal["sale_price"], deal["list_price"])
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
    # Price-history context from Supabase (see pipeline._process_deals) —
    # only ever a fact the model is told, never something it has to infer.
    if deal.get("is_new_low"):
        prompt_lines.append("Price history: this is a new all-time low for this exact item.")
    elif deal.get("lowest_price") is not None and deal["lowest_price"] < deal["sale_price"]:
        prompt_lines.append(f"Price history: the lowest ever tracked for this item was ${deal['lowest_price']:.2f}.")
    prompt_lines.append("")
    prompt_lines.append("Write the verdict.")
    user_prompt = "\n".join(prompt_lines)

    budget = caption_budget(deal["url"])  # 297 - len(url); body must fit with URL line
    system_prompt = config.OPENROUTER_CAPTION_SYSTEM_PROMPT
    system_prompt += f"\n\nThe link is added automatically. Keep your entire output (including hashtags) under {budget} characters total."

    for model in (config.OPENROUTER_PRIMARY_MODEL, config.OPENROUTER_FALLBACK_MODEL):
        caption = _call_openrouter(
            model, system_prompt, user_prompt,
            # "Explain why this is actually noteworthy" is a more
            # demanding ask than the old "write an engaging caption" —
            # confirmed in testing this needs more headroom than 350
            # tokens even at "low" reasoning effort, or it truncates
            # mid-sentence before finishing (still fractions of a cent).
            temperature=0.4, reasoning={"effort": "low"}, max_tokens=600,
        )
        if caption and len(caption) <= budget and _hashtags_look_reasonable(caption):
            return caption
        if caption:
            print(f"[openrouter] {model} caption failed validation (len={len(caption)}, budget={budget}), trying next")

    return build_x_caption_body(deal)  # last resort: mechanical template