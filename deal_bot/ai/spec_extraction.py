"""Clean title + spec extraction via OpenRouter.

Turns a messy retail title into a concise product name plus up to 4 short
technical specs, feeding both the Discord embed and the caption prompt.

Model chain: primary (structured-output capable) with `response_format`, then
the fallback model *without* `response_format` (it doesn't support it), then
raw title + empty specs — fails open at every stage.
"""

import json
import re

from deal_bot import config
from deal_bot.ai.client import _call_openrouter

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# Batched analog of SPEC_EXTRACTION_SYSTEM_PROMPT (in config): same rules,
# but for a numbered list, returned as a single JSON object of items.
_BATCH_SPEC_SYSTEM_PROMPT = """You clean up messy retail product titles for a deal-finding bot focused on PC hardware and electronics. You'll be given a numbered list of raw titles. For each, extract a clean, concise product name and up to 4 short technical specs.

Rules:
- Never invent a spec that isn't explicitly present or clearly implied in the input. If there is genuinely nothing worth calling out, use an empty specs list.
- clean_title: under 100 characters.
- specs: 0 to 4 short strings, each under 60 characters (e.g. "Capacity: 2TB").

Respond with only a JSON object in this exact shape, with EXACTLY one item per input line, in the same order:
{"items": [{"clean_title": string, "specs": [string, ...]}, ...]}"""


def _parse_json_object(content: str | None) -> dict | None:
    """Try to turn model output into a JSON object, twice: a direct parse,
    then a lenient parse (find the first `{...}` block) for fallback models
    that return JSON wrapped in prose or markdown."""
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    match = _JSON_OBJECT_RE.search(content)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _validate_result(title: str, parsed: dict) -> dict:
    """Per-field validation identical to the historical extract_clean_specs —
    a bad clean_title falls back to the raw title, bad specs fall back to
    empty, independently of each other."""
    clean_title = parsed.get("clean_title")
    if isinstance(clean_title, str) and clean_title.strip() and len(clean_title.strip()) <= 100:
        clean_title = clean_title.strip()
    else:
        print(f"[openrouter] spec extraction clean_title failed validation: {clean_title!r}")
        clean_title = title

    specs = parsed.get("specs")
    if (isinstance(specs, list) and len(specs) <= 4
            and all(isinstance(s, str) and s.strip() and len(s.strip()) <= 60 for s in specs)):
        specs = [s.strip() for s in specs]
    else:
        print(f"[openrouter] spec extraction specs failed validation: {specs!r}")
        specs = []

    return {"clean_title": clean_title, "specs": specs}


def extract_clean_specs(title: str, description: str = "") -> dict:
    """Cleans up a messy retail title into a concise product name plus up
    to 4 short technical specs, via OpenRouter. Fails open at every stage:
    no API key, network errors, malformed JSON, an unusable top-level
    response, or a single bad field all fall back to safe defaults without
    ever blocking a post. Must never be able to block a post."""
    fallback = {"clean_title": title, "specs": []}

    user_prompt = f"Title: {title}"
    if description:
        user_prompt += f"\nDescription: {description}"

    # Try the primary model, then the fallback model. Both support
    # structured output, so request JSON directly from each; the lenient
    # parse in _parse_json_object covers any model that ignores it.
    content = None
    for model in (config.OPENROUTER_SPEC_EXTRACTION_MODEL, config.OPENROUTER_SPEC_FALLBACK_MODEL):
        content = _call_openrouter(
            model, config.SPEC_EXTRACTION_SYSTEM_PROMPT, user_prompt,
            temperature=0.0, max_tokens=200, timeout=5,
            response_format={"type": "json_object"},
            # Deliberately omitted: reasoning. Historically the Gemini model
            # burned its entire token budget on internal reasoning when any
            # effort level was set (returning truncated garbage instead of
            # JSON); omitting the parameter entirely is what made it
            # reliable. Qwen 3.7 Flash works either way, so we keep it
            # omitted — simplest and cheapest. Re-verify empirically before
            # setting it for a new model.
        )
        if content:
            break

    parsed = _parse_json_object(content)
    if parsed is None:
        print(f"[openrouter] spec extraction response didn't parse as a JSON object: {content!r}")
        return fallback

    return _validate_result(title, parsed)


def extract_clean_specs_batch(titles: list[str]) -> list[dict]:
    """Batched version of extract_clean_specs — one call for N titles instead
    of N sequential calls (the dominant per-run latency/cost as volume climbs).
    Per-item validation is identical. If the batched response doesn't parse to
    a clean per-item list, falls back to the individual per-item calls so a
    degraded batch never produces worse output than today (worst case equals
    the previous behavior)."""
    if not titles:
        return []
    if not config.OPENROUTER_API_KEY:
        return [{"clean_title": t, "specs": []} for t in titles]

    user_prompt = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    max_tokens = min(4000, 400 + len(titles) * 60)
    content = None
    for model in (config.OPENROUTER_SPEC_EXTRACTION_MODEL, config.OPENROUTER_SPEC_FALLBACK_MODEL):
        content = _call_openrouter(
            model, _BATCH_SPEC_SYSTEM_PROMPT, user_prompt,
            temperature=0.0, max_tokens=max_tokens, timeout=30,
            response_format={"type": "json_object"},
        )
        if content:
            break

    parsed = _parse_json_object(content) or {}
    items = parsed.get("items")
    if (isinstance(items, list) and len(items) == len(titles)
            and all(isinstance(x, dict) for x in items)):
        return [_validate_result(t, item) for t, item in zip(titles, items)]

    # The batch didn't come back structurally clean — fall back to the
    # per-item function so each title still gets its own extraction attempt.
    print("[openrouter] batch spec extraction unusable — falling back to per-item")
    return [extract_clean_specs(t) for t in titles]