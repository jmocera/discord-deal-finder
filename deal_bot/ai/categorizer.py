"""Category tagger — SHADOW MODE (not gating or routing posts yet).

One batched OpenRouter call per run tagging each deal into a fine-grained
category (storage/display/component/peripheral/game/other), which could
later drive per-category Discord channels or better hashtag targeting.
"""

import re

from deal_bot import config
from deal_bot.ai.client import _call_openrouter


def _category_line_pattern() -> re.Pattern:
    """Built per call (not at import) from config.DEAL_CATEGORIES — same
    call-time-config convention as the rest of the package, so a test (or
    operator) mutating DEAL_CATEGORIES takes effect without re-importing.
    Called at most twice per run; compile cost is negligible."""
    return re.compile(
        r"(?im)^\s*(?:[-–—*•]\s+|\d+[.):]\s+)?("
        + "|".join(re.escape(c) for c in config.DEAL_CATEGORIES)
        + r")\s*$"
    )


def _extract_categories(response: str) -> list[str]:
    """Line-anchored known-category extraction. Each non-empty line must
    contain *only* a category word (with optional bullet/numbered prefix);
    any other content on the line rejects it. Matching is case-insensitive
    (the `(?i)` flag); results are lowercased so the caller can compare
    against `config.DEAL_CATEGORIES` verbatim.

    Replaces the previous greedy word-boundary `findall`, which would lift
    `game` out of `Game Controller` or `storage` out of `storage device`.
    The caller now requires `len(extracted) == len(deals)`.
    """
    return [m.lower() for m in _category_line_pattern().findall(response)]


def categorize_deals(deals: list[dict]) -> tuple[dict[str, str], str | None]:
    """Returns ({deal_id: category}, model_used). One batched call, fail-open.

    Fails OPEN: on any failure (missing key, both models erroring, or a
    response that cannot be parsed to exactly len(deals) line-anchored
    category tokens) an empty map and None are returned — the caller
    treats that as "no categories this run," never as a reason to drop a
    deal. Partial salvage is intentionally removed: a response that
    doesn't yield exactly one category per deal for every deal falls
    through to the fallback model.
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

        # Strict path: one clean category per line, exactly len(deals) lines.
        categories = [line.strip().lower() for line in response.strip().splitlines() if line.strip()]
        if len(categories) == len(deals) and all(c in valid for c in categories):
            return {d["id"]: c for d, c in zip(deals, categories)}, model

        # Lenient path: line-anchored regex extraction. Accepts bullets,
        # numbered prefixes, and case variants. Requires the extracted
        # token count to EXACTLY equal len(deals) — no partial salvage.
        extracted = _extract_categories(response)
        if len(extracted) == len(deals):
            return {d["id"]: c for d, c in zip(deals, extracted)}, model
        print(
            f"[openrouter] categorizer response from {model} yielded "
            f"{len(extracted)} categories for {len(deals)} deals — trying next"
        )

    print("[openrouter] categorizer unavailable from both models this run — no shadow report")
    return {}, None
