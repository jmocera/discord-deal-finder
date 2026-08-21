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