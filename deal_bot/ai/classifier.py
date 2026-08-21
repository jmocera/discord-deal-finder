"""Shadow-mode desirability classifier — not gating real posts yet."""

from deal_bot import config
from deal_bot.ai.client import _call_openrouter


def classify_desirable_deals(deals: list[dict]) -> tuple[list[dict], list[dict], str | None]:
    """SHADOW MODE (not gating anything yet). One batched OpenRouter call
    judging whether each deal is genuinely desirable to a PC-building/
    gaming audience, beyond just having cleared the keyword/discount
    filters. Returns (keep, drop, model_used).

    Fails OPEN: if both models error or the response doesn't parse
    cleanly (wrong line count, anything other than KEEP/DROP), everything
    is kept. A wrong KEEP is a mediocre post; a wrong DROP would be an
    invisible lost deal — once/if this becomes a real gate, keeping
    everything is the safer failure direction. In shadow mode a failed
    call just means no report gets sent this run."""
    if not deals:
        return [], [], None
    if not config.OPENROUTER_API_KEY:
        return list(deals), [], None

    lines = [
        f"{i}. [{d['source']}] {d['title']} — {d['discount_pct']}% off, ${d['sale_price']:.2f}"
        for i, d in enumerate(deals, start=1)
    ]
    user_prompt = "\n".join(lines)
    # A generous floor, not just a per-item scale-up: reasoning overhead
    # doesn't shrink proportionally with a smaller item count, and a
    # too-tight budget reproduces the same null-content failure the
    # caption path hit before its fix — confirmed in testing.
    max_tokens = min(1500, 300 + len(deals) * 15)

    for model in (config.OPENROUTER_PRIMARY_MODEL, config.OPENROUTER_FALLBACK_MODEL):
        response = _call_openrouter(
            model, config.OPENROUTER_CLASSIFIER_SYSTEM_PROMPT, user_prompt,
            temperature=0.1, max_tokens=max_tokens, reasoning={"effort": "low"},
        )
        if not response:
            continue
        verdicts = [line.strip().upper() for line in response.strip().splitlines() if line.strip()]
        if len(verdicts) != len(deals) or any(v not in ("KEEP", "DROP") for v in verdicts):
            print(
                f"[openrouter] classifier response from {model} didn't parse cleanly "
                f"({len(verdicts)} lines for {len(deals)} deals) — trying next"
            )
            continue
        keep = [d for d, v in zip(deals, verdicts) if v == "KEEP"]
        drop = [d for d, v in zip(deals, verdicts) if v == "DROP"]
        return keep, drop, model

    print("[openrouter] classifier unavailable from both models this run — no shadow report")
    return list(deals), [], None