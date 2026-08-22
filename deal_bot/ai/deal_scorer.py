"""Deal quality scorer — SHADOW MODE (not gating real posts yet).

One batched OpenRouter call per run rating each deal 1-10 for a
PC-building/gaming audience, complementing the keyword/discount filters with
a judgment of whether the item is genuinely *desirable* (recognizable brand,
real spec-to-price value) rather than merely topically in-category.
"""

import re

from deal_bot import config
from deal_bot.ai.client import _call_openrouter

# The bullet branch ([-–—*•]\s+) deliberately also accepts a negative-looking
# "- 5" as score 5 — the same shape as the "- 9" markdown bullet; scores are
# never negative in the 1-10 rubric, so this is a no-op in practice.
_SCORE_LINE = re.compile(
    r"(?m)^\s*(?:[-–—*•]\s+|\d+[.):]\s+)?(10|[1-9])(?:\s*/\s*10)?\s*$"
)


def _extract_scores(response: str) -> list[int]:
    """Line-anchored 1-10 score extraction. Each non-empty line must
    contain *only* a score (with optional bullet/numbered prefix and an
    optional `/10` suffix); any other content on the line rejects it.

    Replaces the previous greedy `findall` over the whole response, which
    would silently lift scores out of contaminated narration
    ("WD 10TB ... $159.99. 9"). The caller now requires
    `len(extracted) == len(deals)` — partial salvage is intentionally gone.
    """
    return [int(m) for m in _SCORE_LINE.findall(response)]


def score_deals(deals: list[dict]) -> tuple[dict[str, int], str | None]:
    """Returns ({deal_id: score}, model_used). One batched call, fail-open.

    Fails OPEN: if both models error or the response cannot be parsed to
    exactly len(deals) line-anchored scores, an empty score map and None
    are returned — which the caller treats as "everything passes," since
    a wrong DROP would be an invisible lost deal while a wrong KEEP is
    just a visible, ignorable post. Partial salvage is intentionally
    removed: a response that doesn't yield exactly one score per deal
    for every deal falls through to the fallback model.
    """
    if not deals:
        return {}, None
    if not config.OPENROUTER_API_KEY:
        return {}, None

    lines = [
        f"{i}. [{d['source']}] {d['title']} — {d['discount_pct']}% off, ${d['sale_price']:.2f}"
        for i, d in enumerate(deals, start=1)
    ]
    user_prompt = "\n".join(lines)
    max_tokens = min(1500, 300 + len(deals) * 15)

    for model in (config.OPENROUTER_QUALITY_SCORER_MODEL, config.OPENROUTER_QUALITY_SCORER_FALLBACK_MODEL):
        response = _call_openrouter(
            model, config.OPENROUTER_QUALITY_SCORER_SYSTEM_PROMPT, user_prompt,
            temperature=0.1, max_tokens=max_tokens,
            # reasoning deliberately omitted: Gemma 4 26B burns its whole
            # token budget on internal reasoning when any effort is set
            # (confirmed empirically — returns null content), the opposite of
            # the caption/classifier models which need {"effort": "low"}.
            # Per the project's standing rule: test per-model, never assume.
        )
        if not response:
            continue

        # Strict path: one clean score per line, exactly len(deals) lines.
        scores: list[int] = []
        ok = True
        for line in response.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                score = int(line)
            except ValueError:
                ok = False
                break
            if not 1 <= score <= 10:
                ok = False
                break
            scores.append(score)
        if ok and len(scores) == len(deals):
            return {d["id"]: score for d, score in zip(deals, scores)}, model

        # Lenient path: line-anchored regex extraction. Accepts bullets,
        # numbered prefixes, and `/10` suffixes. Requires the extracted
        # token count to EXACTLY equal len(deals) — no partial salvage.
        extracted = _extract_scores(response)
        if len(extracted) == len(deals):
            return {d["id"]: score for d, score in zip(deals, extracted)}, model
        print(
            f"[openrouter] quality scorer response from {model} yielded "
            f"{len(extracted)} scores for {len(deals)} deals — trying next"
        )

    print("[openrouter] quality scorer unavailable from both models this run — no shadow report")
    return {}, None