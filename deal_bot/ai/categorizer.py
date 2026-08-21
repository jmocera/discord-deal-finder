"""Category tagger — SHADOW MODE (not gating or routing posts yet).

One batched OpenRouter call per run tagging each deal into a fine-grained
category (storage/display/component/peripheral/game/other), which could
later drive per-category Discord channels or better hashtag targeting.
"""

from deal_bot import config
from deal_bot.ai.client import _call_openrouter


def categorize_deals(deals: list[dict]) -> tuple[dict[str, str], str | None]:
    """Returns ({deal_id: category}, model_used). One batched call, fail-open.

    Fails OPEN: on any failure (missing key, both models erroring, or a
    response that doesn't parse cleanly) an empty map and None are returned
    — the caller treats that as "no categories this run," never as a reason
    to drop a deal.
    """
    if not deals:
        return {}, None
    if not config.OPENROUTER_API_KEY:
        return {}, None

    lines = [
        f"{i}. [{d['source']}] {d['title']} — ${d['sale_price']:.2f}"
        for i, d in enumerate(deals, start=1)
    ]
    user_prompt = "\n".join(lines)
    max_tokens = min(1000, 200 + len(deals) * 15)

    valid = set(config.DEAL_CATEGORIES)
    for model in (config.OPENROUTER_CATEGORIZER_MODEL, config.OPENROUTER_CATEGORIZER_FALLBACK_MODEL):
        response = _call_openrouter(
            model, config.OPENROUTER_CATEGORIZER_SYSTEM_PROMPT, user_prompt,
            temperature=0.1, max_tokens=max_tokens,
            # reasoning omitted: Gemma burns its token budget on reasoning
            # when any effort is set (see ai/deal_scorer.py).
        )
        if not response:
            continue

        categories = [line.strip().lower() for line in response.strip().splitlines() if line.strip()]
        if len(categories) != len(deals) or any(c not in valid for c in categories):
            print(f"[openrouter] categorizer response from {model} didn't parse cleanly — trying next")
            continue
        return {d["id"]: c for d, c in zip(deals, categories)}, model

    print("[openrouter] categorizer unavailable from both models this run — no shadow report")
    return {}, None
