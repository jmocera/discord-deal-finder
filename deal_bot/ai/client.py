"""Shared OpenRouter client.

A single `_call_openrouter()` used by every AI feature in this package (and
by `vet_amazon_deal.py`), so the request shape, fail-open behavior, and
model-specific gotchas live in exactly one place.
"""

import re

from deal_bot import config, transport


def _call_openrouter(
    model: str, system_prompt: str, user_prompt: str,
    *, temperature: float = 0.8, max_tokens: int = 350,
    reasoning: dict | None = None, response_format: dict | None = None,
    timeout: int = 20,
) -> str | None:
    """One chat-completions call, failing open (returns None) on any problem.

    `user_prompt` can be a plain string or a list of OpenRouter content
    blocks (for multimodel/vision calls), matching the wire format of the
    `"content"` field. `reasoning` and `response_format` are opt-in per
    call, not defaulted — different models on OpenRouter behave oppositely
    here (see HANDOFF.md bugs #7/#8): some reasoning-capable models burn
    their whole token budget on internal reasoning unless `effort` is set;
    others break if *any* effort is set. So every caller states explicitly
    what it needs rather than this function guessing for all models.

    Transient failures (network blips, 429/5xx) are retried by the shared
    transport; this still fails open (returns None) after that, so the
    caller's fallback chain (next model / plain template) is unaffected.
    """
    if not config.OPENROUTER_API_KEY:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if reasoning is not None:
        payload["reasoning"] = reasoning
    if response_format is not None:
        payload["response_format"] = response_format

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = transport.request(
        "POST", "https://openrouter.ai/api/v1/chat/completions",
        headers=headers, json=payload, timeout=timeout,
    )
    if resp is None:
        print(f"[openrouter] request failed for model {model} after retries")
        return None
    if resp.status_code != 200:
        print(f"[openrouter] model {model} returned {resp.status_code}: {resp.text[:300]}")
        return None
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError, TypeError) as e:
        print(f"[openrouter] unexpected response shape from {model}: {e}")
        return None
    if not content:
        # Present-but-null/empty content — e.g. a reasoning model that
        # burned its whole budget on the reasoning trace and never got to
        # write an answer. Not an exception, just "no usable output."
        print(f"[openrouter] {model} returned no usable content (reasoning may have exhausted the token budget)")
        return None
    content = content.strip()
    # Some models wrap JSON in a markdown code fence despite being told
    # not to (especially ones without structured-output support) — strip
    # one if present rather than rejecting the whole thing.
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
    # Small/free models occasionally ignore "no quotation marks" — strip a
    # single wrapping pair if present rather than rejecting the whole thing.
    content = content.strip('"').strip("'").strip()
    return content or None


# Public alias for consumers OUTSIDE the deal_bot package (e.g. the
# standalone vet_amazon_deal.py script) — internal package modules keep
# importing the underscore name, and tests keep patching it.
call_openrouter = _call_openrouter