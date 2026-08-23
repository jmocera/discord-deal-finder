"""Post-length fitting — "format a social post to a grapheme budget".

Bluesky caps posts at 300 grapheme clusters; our captions add hashtags and
a link, so the whole assembled string can outgrow that. This module owns the
single source of truth for the budget and the fitting logic, reused by the
Bluesky integration and the vet CLI preview so a post that fits in one place
fits everywhere.

Enforcement is by Python len() (code points), not graphemes: graphemes are
always <= code points, so a code-point budget is strictly server-safe. A
best-effort grapheme_len() exists only for tests/logging (it never gates
anything) — see grapheme_len() for why we don't add the `regex` dependency
to do this properly.
"""

import re

from deal_bot import config

_HASHTAG_PATTERN = re.compile(r"#(\w+)")

ELLIPSIS = "…"


def hard_target() -> int:
    """The code-point budget for one Bluesky post, read at call time so
    tests (and operators via .env) can change config.BLUESKY_MAX_POST_LEN /
    BLUESKY_POST_MARGIN without re-importing this module — same convention
    as every other config consumer in the package."""
    return config.BLUESKY_MAX_POST_LEN - config.BLUESKY_POST_MARGIN


def grapheme_len(s: str) -> int:
    """Best-effort grapheme-cluster count (combining marks, ZWJ sequences,
    VS16/emoji modifiers, and regional-indicator flag pairs count as one).

    Informational only — never the enforcement metric. Real grapheme
    segmentation (the `regex` library's \\X) would add a dependency for a
    few chars of headroom we don't need, because len() (code points) is
    already a strict upper bound and therefore server-safe."""
    count = 0
    prev_was_zwj = False
    ri_parity = 0  # consecutive regional indicators seen so far, mod 2
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\r" and i + 1 < n and s[i + 1] == "\n":
            count += 1
            prev_was_zwj = False
            ri_parity = 0
            i += 2
            continue
        if _is_combining(ch) or _is_variation_selector(ch) or prev_was_zwj:
            pass  # joins the previous grapheme
        elif _is_regional_indicator(ch) and ri_parity == 1:
            ri_parity = 0  # second half of a flag pair
        else:
            count += 1
            ri_parity = 1 if _is_regional_indicator(ch) else 0
        prev_was_zwj = (ch == "\u200D")
        i += 1
    return count


def _is_combining(ch: str) -> bool:
    return "\u0300" <= ch <= "\u036F" or "\u1AB0" <= ch <= "\u1AFF" or "\u20D0" <= ch <= "\u20FF"

def _is_variation_selector(ch: str) -> bool:
    return "\uFE00" <= ch <= "\uFE0F"

def _is_regional_indicator(ch: str) -> bool:
    return "\U0001F1E6" <= ch <= "\U0001F1FF"


def truncate_to(text: str, limit: int, *, ellipsis: str = ELLIPSIS) -> str:
    """Hard tail-truncate to at most `limit` code points, appending an
    ellipsis if anything was cut. For URL-free text (weekly digest) or a
    last resort — prefer fit_deal_post() when a hashtag tail matters."""
    if len(text) <= limit:
        return text
    return text[: limit - len(ellipsis)].rstrip() + ellipsis


def caption_budget(url: str | None) -> int:
    """Max caption-body length (code points) so body + newline + url still
    fits the hard target. The -1 accounts for the '\n' before the URL."""
    if not url:
        return hard_target()
    return hard_target() - 1 - len(url)


def fit_deal_post(body: str, url: str | None) -> str:
    """Assemble a deal post: body (prose + hashtags) plus an optional URL.

    Fitting priority, by construction:
      1. URL always preserved, always the last line.
      2. Hashtag tail preserved whenever possible — trim the PROSE.
      3. Drop hashtags one-by-one (from the end) only as a last resort.
      4. Drop the ellipsis, then degrade to URL-only, then (never) raise.

    Always returns a string of len() <= hard_target() (code points), never
    empty, never raises.
    """
    target = hard_target()
    if not url:
        return truncate_to(body, target)

    url_suffix = "\n" + url

    # Happy path — no ellipsis, hashtags intact.
    if len(body) + len(url_suffix) <= target:
        return body + url_suffix

    prose, hashtags = _split_hashtag_block(body)
    sep = 1 if hashtags else 0  # single space before the hashtag block
    fixed = sep + len(hashtags) + len(url_suffix)

    # 1) Trim the prose first, keep all hashtags.
    n = target - fixed - len(ELLIPSIS)
    if n >= 1:
        return _trim_prose(prose, n) + ELLIPSIS + (f" {hashtags}" if hashtags else "") + url_suffix

    # 2) Drop hashtags one-by-one (end first), keep the ellipsis.
    tags = hashtags.split() if hashtags else []
    while tags:
        tags.pop()
        hb = " ".join(tags)
        fixed = (1 if hb else 0) + len(hb) + len(url_suffix)
        n = target - fixed - len(ELLIPSIS)
        if n >= 1:
            print("[post_len] dropped hashtag(s) to fit post")
            return _trim_prose(prose, n) + ELLIPSIS + (f" {hb}" if hb else "") + url_suffix

    # 3) No hashtags fit — drop the ellipsis too.
    n = target - len(url_suffix)
    if n >= 1:
        print("[post_len] warning: hashtags dropped entirely; ellipsis dropped")
        return _trim_prose(prose, n) + url_suffix

    # 4) Degenerate: the URL alone leaves no room for text.
    print("[post_len] warning: URL leaves no room for text; posting link only")
    return url


def _split_hashtag_block(text: str) -> tuple[str, str]:
    """Split off a trailing, whitespace-separated run of hashtags.

    Only a *contiguous* run at the very tail counts as the "block" — a tag
    mid-sentence (e.g. "This #SSD is great #Deals") stays in the prose,
    which is correct: it's part of the sentence, not a tag tail."""
    stripped = text.rstrip()
    if not stripped:
        return text, ""
    tokens = stripped.split()
    tags = []
    for token in reversed(tokens):
        if _is_hashtag_block(token):
            tags.append(token)
        else:
            break
    if not tags:
        return stripped, ""
    tags.reverse()
    tag_block = " ".join(tags)
    idx = stripped.rfind(tag_block)
    prose = stripped[:idx].rstrip()
    return prose, tag_block


def _is_hashtag_block(token: str) -> bool:
    return bool(token) and _HASHTAG_PATTERN.fullmatch(token) is not None


def _trim_prose(prose: str, n: int) -> str:
    """Trim prose to at most n code points, preferring word boundaries and
    never splitting a leading #hashtag token (e.g. the '#ad' disclosure
    that must survive every post). If even that token can't fit intact,
    returns "" rather than slicing it mid-token."""
    if n <= 0:
        return ""
    if len(prose) <= n:
        return prose
    lead = re.match(r"#[A-Za-z0-9]+\s+", prose)
    if lead:
        prefix = lead.group(0)
        if n <= len(prefix.rstrip()):
            return ""  # can't fit the tag intact — drop the prose entirely
        return prefix + _trim_prose(prose[len(prefix):], n - len(prefix)).rstrip()
    cut = prose[:n].rstrip()
    space = cut.rfind(" ")
    if space > 0:
        return cut[:space]
    return cut or prose[:n]  # giant single token -> hard slice