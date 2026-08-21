"""Bluesky posting — raw AT Protocol REST/XRPC calls (no SDK, consistent
with the rest of the project's minimal-dependency approach)."""

from datetime import datetime, timezone

import requests

from deal_bot import config
from deal_bot.ai.captions import _HASHTAG_PATTERN, build_ai_caption

_bluesky_session = None  # cached for the duration of one run, avoids re-login per post


def _bluesky_login() -> dict | None:
    global _bluesky_session
    if _bluesky_session:
        return _bluesky_session
    if not config.BLUESKY_HANDLE or not config.BLUESKY_APP_PASSWORD:
        return None
    try:
        resp = requests.post(
            "https://bsky.social/xrpc/com.atproto.server.createSession",
            json={"identifier": config.BLUESKY_HANDLE, "password": config.BLUESKY_APP_PASSWORD},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[bluesky] login failed: {e}")
        return None
    _bluesky_session = resp.json()
    return _bluesky_session


def _build_tag_facets(text: str) -> list[dict]:
    """One app.bsky.richtext.facet#tag per #hashtag in text. Byte offsets
    (not character offsets) computed the same way as the URL link facet
    below — encode the prefix up to each match to correctly account for
    any multi-byte characters (em dashes, accents) earlier in the text."""
    facets = []
    for match in _HASHTAG_PATTERN.finditer(text):
        tag_name = match.group(1)
        byte_start = len(text[:match.start()].encode("utf-8"))
        byte_end = len(text[:match.end()].encode("utf-8"))
        facets.append({
            "index": {"byteStart": byte_start, "byteEnd": byte_end},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag_name}],
        })
    return facets


def _build_bluesky_embed(session: dict, deal: dict) -> dict | None:
    """Downloads the deal's image and uploads it as a blob for a rich
    external-link preview card. Fails open at every step — no image URL,
    a download error, a non-image response, or an uploadBlob failure all
    just mean no card; post_to_bluesky() still sends the post as plain
    text+facets either way."""
    image_url = deal.get("image")
    if not image_url:
        return None

    try:
        img_resp = requests.get(image_url, timeout=10)
        img_resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[bluesky] thumbnail download failed: {e}")
        return None

    content_type = img_resp.headers.get("Content-Type", "").split(";")[0].strip()
    if not content_type.startswith("image/"):
        print(f"[bluesky] thumbnail skipped — unexpected content-type {content_type!r}")
        return None

    try:
        blob_resp = requests.post(
            "https://bsky.social/xrpc/com.atproto.repo.uploadBlob",
            headers={
                "Authorization": f"Bearer {session['accessJwt']}",
                "Content-Type": content_type,
            },
            data=img_resp.content,
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"[bluesky] thumbnail upload failed: {e}")
        return None
    # Covers oversized images too — the PDS rejects those with a non-200
    # rather than us needing to guess its exact size cap up front.
    if blob_resp.status_code != 200:
        print(f"[bluesky] thumbnail upload returned {blob_resp.status_code}: {blob_resp.text[:300]}")
        return None

    try:
        blob = blob_resp.json()["blob"]
    except (KeyError, ValueError, TypeError) as e:
        print(f"[bluesky] unexpected uploadBlob response shape: {e}")
        return None

    description = f"${deal['sale_price']:.2f}"
    if deal["list_price"]:
        description = f"Now ${deal['sale_price']:.2f} (was ${deal['list_price']:.2f})"

    return {
        "$type": "app.bsky.embed.external",
        "external": {
            "uri": deal["url"],
            "title": deal["title"][:300],
            "description": description,
            "thumb": blob,
        },
    }


def post_to_bluesky(deal: dict) -> bool:
    session = _bluesky_login()
    if not session:
        return False

    text = build_ai_caption(deal)  # AI-written when available, template on fallback
    if len(text) > 300:  # Bluesky's post length limit
        # Trim the caption body, not a blind tail-slice of the whole
        # string — the URL sits on the last line, and slicing the whole
        # thing could clip or remove it, silently breaking the link
        # facet below for anything long enough to need truncating.
        url_suffix = f"\n{deal['url']}"
        body = text[:-len(url_suffix)] if text.endswith(url_suffix) else text
        max_body_len = 300 - len(url_suffix) - 1  # -1 for the trailing "…"
        text = body[:max_body_len] + "…" + url_suffix

    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    # AT Protocol doesn't auto-linkify plain URLs in post text the way
    # most social apps do — without an explicit "facet" marking the byte
    # range and its target, a URL renders as inert plain text (exactly
    # what was happening). Byte offsets, not character offsets: facets
    # are defined over the UTF-8-encoded text, and this caption can
    # contain multi-byte characters (e.g. the em dash) before the URL.
    facets = []
    url_bytes = deal["url"].encode("utf-8")
    text_bytes = text.encode("utf-8")
    idx = text_bytes.find(url_bytes)
    if idx != -1:
        facets.append({
            "index": {"byteStart": idx, "byteEnd": idx + len(url_bytes)},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": deal["url"]}],
        })
    facets.extend(_build_tag_facets(text))
    if facets:
        facets.sort(key=lambda f: f["index"]["byteStart"])
        record["facets"] = facets

    embed = _build_bluesky_embed(session, deal)
    if embed:
        record["embed"] = embed

    try:
        resp = requests.post(
            "https://bsky.social/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[bluesky] post failed: {e}")
        return False

    if resp.status_code != 200:
        print(f"[bluesky] post returned {resp.status_code}: {resp.text[:300]}")
        return False
    return True


def post_text_to_bluesky(text: str) -> bool:
    """Post plain text (e.g. the weekly digest) to Bluesky. Truncates to
    300 chars. No link facet or embed — a digest has no single URL to link."""
    session = _bluesky_login()
    if not session:
        return False

    if len(text) > 300:
        text = text[:296] + "…"

    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        resp = requests.post(
            "https://bsky.social/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[bluesky] post failed: {e}")
        return False

    if resp.status_code != 200:
        print(f"[bluesky] post returned {resp.status_code}: {resp.text[:300]}")
        return False
    return True