"""Central configuration for the deal bot.

Every environment variable and tuning constant lives here, so the rest of
the package imports from `deal_bot.config` instead of reading `os.environ`
or hardcoding numbers in scattered places. Other modules reference values
at call time (e.g. `config.MIN_DISCOUNT_PERCENT`) rather than importing them
as names, so tests can monkeypatch a config attribute and have it take
effect without re-importing the module.

Values come from the `.env` file in the repo root locally, or from real
environment variables (GitHub Actions repo secrets/variables) on schedule.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Loads variables from the repo-root `.env` file into the environment. In
# GitHub Actions there is no .env file — the same names are injected as
# real environment variables from repo secrets/variables instead, and this
# call is simply a no-op there.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ---------------------------------------------------------------------------
# API / service credentials
# ---------------------------------------------------------------------------
WOOT_API_KEY = os.environ.get("WOOT_API_KEY", "")
BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# ---------------------------------------------------------------------------
# Discord webhooks
# ---------------------------------------------------------------------------
WOOT_WEBHOOK_URL = os.environ.get("WOOT_WEBHOOK_URL", "")
BESTBUY_WEBHOOK_URL = os.environ.get("BESTBUY_WEBHOOK_URL", "")
# Steam's public storefront "Specials" data — no API key needed, this is
# the same data that powers Steam's own front-page deals section.
STEAM_WEBHOOK_URL = os.environ.get("STEAM_WEBHOOK_URL", "")
# Optional: mirrors every deal that posts publicly into a private,
# owner-only channel — handy as a staging area for manually picking what
# to share elsewhere. Leave unset to skip this entirely.
PRIVATE_WEBHOOK_URL = os.environ.get("PRIVATE_WEBHOOK_URL", "")
# Dedicated channel for the end-of-run digest, separate from the per-deal
# source channels above.
DIGEST_WEBHOOK_URL = os.environ.get("DIGEST_WEBHOOK_URL", "")
# Dedicated channel that mirrors every run_log row (see pipeline.log_run) —
# posts every run, success or failure, so a crash is actually visible
# somewhere instead of only being a silent row in Supabase.
RUN_LOG_WEBHOOK_URL = os.environ.get("RUN_LOG_WEBHOOK_URL", "")
# SHADOW MODE: reports what the desirability classifier would have
# kept/dropped, without actually gating real posts on it yet.
SHADOW_CLASSIFIER_WEBHOOK_URL = os.environ.get("SHADOW_CLASSIFIER_WEBHOOK_URL", "")
# SHADOW MODE: reports the deal quality scorer's 1-10 ratings (and what it
# would have dropped below MIN_QUALITY_SCORE), without actually gating posts.
SHADOW_QUALITY_SCORER_WEBHOOK_URL = os.environ.get("SHADOW_QUALITY_SCORER_WEBHOOK_URL", "")
# SHADOW MODE: reports the category tagger's per-deal classification,
# without actually using it to gate or route posts yet.
SHADOW_CATEGORIZER_WEBHOOK_URL = os.environ.get("SHADOW_CATEGORIZER_WEBHOOK_URL", "")

# ---------------------------------------------------------------------------
# Bluesky — free API, no approval process. Only standout deals auto-post
# here (see BLUESKY_MIN_DISCOUNT_PERCENT below), and even among those,
# only the top BLUESKY_MAX_POSTS_PER_RUN by $ saved actually go out — to
# avoid looking like a spam firehose on a brand-new account.
# ---------------------------------------------------------------------------
BLUESKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "")
BLUESKY_APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "")
BLUESKY_MIN_DISCOUNT_PERCENT = int(os.environ.get("BLUESKY_MIN_DISCOUNT_PERCENT", "50"))
BLUESKY_MAX_POSTS_PER_RUN = int(os.environ.get("BLUESKY_MAX_POSTS_PER_RUN", "2"))

# ---------------------------------------------------------------------------
# OpenRouter — AI-written captions for Bluesky and the private-channel
# copy-paste mirror, replacing the plain template. Tries the primary
# model, then the free fallback model, then the plain template as a last
# resort — this must never be able to block a post from going out.
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_PRIMARY_MODEL = os.environ.get("OPENROUTER_PRIMARY_MODEL", "deepseek/deepseek-v4-flash-0731")
OPENROUTER_FALLBACK_MODEL = os.environ.get("OPENROUTER_FALLBACK_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
OPENROUTER_CAPTION_SYSTEM_PROMPT = """You write short, data-backed technical verdicts for a deal-finding bot aimed at PC builders and PC gamers — not marketing copy. You'll be given a product's clean title, its known specs (if any), current and list price, and price-history context (whether this is a new all-time low, or what the lowest tracked price has been).

Output ONLY the verdict text — no preamble, no explanation, no quotation marks, no markdown formatting, no code fences.

Write exactly 1-2 concise sentences explaining *why* this deal is actually noteworthy — a real price-history signal (e.g. a genuine all-time low), real value-for-money given the specs you were given, or a specific use case those specs support. Take a direct, analytical, enthusiast tone. Do not use hype phrases like "insane deal," "don't miss out," or "act now." Never state a spec, benchmark number, or feature that wasn't explicitly given to you — if you don't have enough information to say something specific and true, keep it simple rather than inventing detail.

Keep the entire output under 200 characters. End with 2 to 4 relevant, space-separated hashtags chosen specifically for this item — vary them based on what the deal actually is, don't reuse the same generic tags every time. Never include a URL or link.

Example, given a 2TB PCIe Gen4 NVMe SSD at a new all-time low of $79.99 (was $159.99):
This is the lowest we've tracked this 2TB PCIe Gen4 drive — a genuine all-time low, not just a markdown. Fast NVMe storage at a real floor price. #PCBuild #SSDDeals #TechDeals"""

# Longer expert "analysis" for the Discord embed (complements the short
# Bluesky caption verdict above — same models, richer output, an optional
# enhancement that fails open to an empty string).
OPENROUTER_ANALYSIS_SYSTEM_PROMPT = """You write short expert analysis for a deal-finding bot aimed at PC builders and PC gamers. Given a product's clean title, known specs (if any), current and list price, and price-history context, write 2-3 concise sentences explaining what makes this deal genuinely noteworthy:

- What kind of build or use case it fits (e.g. boot drive for a budget rig, secondary game library, competitive-esports display).
- Whether the price is strong for the specs given, and what it competes against at that price point.
- Which specific spec(s) actually matter for that use case.

Take a direct, analytical, enthusiast tone. Do not use hype phrases like "insane deal" or "act now." Never state a spec, benchmark, or competitor price that wasn't explicitly given to you — if you don't have enough information to say something specific and true, keep it simple rather than inventing detail.

Output ONLY the analysis text — no preamble, no markdown, no quotation marks, no hashtags, no URL. Keep the entire output under 350 characters."""

# Weekly digest — a once-a-week curated roundup written by AI from the
# week's posted deals (stored in the `posted_deals` Supabase table). Free
# Gemma model, with the paid Gemma as fallback. See weekly_digest.py.
OPENROUTER_WEEKLY_DIGEST_MODEL = os.environ.get("OPENROUTER_WEEKLY_DIGEST_MODEL", "google/gemma-4-26b-a4b-it:free")
OPENROUTER_WEEKLY_DIGEST_FALLBACK_MODEL = os.environ.get("OPENROUTER_WEEKLY_DIGEST_FALLBACK_MODEL", "google/gemma-4-26b-a4b-it")
OPENROUTER_WEEKLY_DIGEST_SYSTEM_PROMPT = """You write a weekly roundup for a deal-finding bot aimed at PC builders and PC gamers. You'll be given a list of the week's posted deals (title, source, sale price, list price, and discount). Pick the top 3-5 most noteworthy and write a short, punchy summary of each: what it is, who it's for, and why the price stood out. Use a direct, analytical, enthusiast tone — no hype phrases like "insane" or "don't miss out." Never state a spec, benchmark, or price that isn't in the input.

Output plain text only — no markdown, no hashtags, no URL. Start with a one-line intro (e.g. "This week's best PC and gaming deals:"). Keep each deal summary to 1-2 sentences. End with a one-line sign-off."""

OPENROUTER_CLASSIFIER_SYSTEM_PROMPT = """You screen deal listings for a bot that posts discounts to an audience of PC-building and PC-gaming enthusiasts. For each numbered item below, decide whether it is something that audience would genuinely want — not just topically related (e.g. "electronics"), but actually desirable: recognizable brands, real PC parts, monitors, peripherals, games, and similar. Reject generic, off-brand, or low-interest items even if they're topically in-category.

Respond with exactly one line per item, in the same order as the input, containing only the word KEEP or DROP — nothing else. No numbering, no explanation, no extra text. The number of output lines must exactly match the number of input items."""

# Deal quality scorer — SHADOW MODE (not gating anything yet). One batched
# call per run rating each deal 1-10 for a PC-building/gaming audience,
# complementing the keyword/discount filters with an AI judgment of whether
# the item is genuinely *desirable* (recognizable brand, real value) rather
# than merely in-category. See ai.deal_scorer.score_deals().
OPENROUTER_QUALITY_SCORER_MODEL = os.environ.get("OPENROUTER_QUALITY_SCORER_MODEL", "google/gemma-4-26b-a4b-it:free")
OPENROUTER_QUALITY_SCORER_FALLBACK_MODEL = os.environ.get("OPENROUTER_QUALITY_SCORER_FALLBACK_MODEL", "google/gemma-4-26b-a4b-it")
MIN_QUALITY_SCORE = int(os.environ.get("MIN_QUALITY_SCORE", "6"))
OPENROUTER_QUALITY_SCORER_SYSTEM_PROMPT = """You score deal listings for a bot that posts discounts to an audience of PC-building and PC-gaming enthusiasts. For each numbered item below, rate how genuinely desirable it is to that audience on a scale of 1 to 10, where 10 is a must-buy and 1 is generic/off-brand junk. Consider: recognizable brand in the PC/gaming space, real spec-to-price value, and whether it is a genuine PC/gaming product rather than something merely topically in-category (e.g. a no-name cable, an off-brand power strip).

Respond with exactly one line per item, in the same order as the input. Each line must be a single integer from 1 to 10 — nothing else. No numbering, no explanation, no extra text. The number of output lines must exactly match the number of input items."""

# Category tagger — SHADOW MODE (not gating or routing yet). One batched
# call per run tagging each deal into a fine-grained category, which could
# later drive per-category Discord channels or better hashtag/analysis
# targeting. See ai.categorizer.categorize_deals().
OPENROUTER_CATEGORIZER_MODEL = os.environ.get("OPENROUTER_CATEGORIZER_MODEL", "google/gemma-4-26b-a4b-it:free")
OPENROUTER_CATEGORIZER_FALLBACK_MODEL = os.environ.get("OPENROUTER_CATEGORIZER_FALLBACK_MODEL", "google/gemma-4-26b-a4b-it")
DEAL_CATEGORIES = ["storage", "display", "component", "peripheral", "game", "other"]
OPENROUTER_CATEGORIZER_SYSTEM_PROMPT = """You classify deal listings for a bot aimed at PC builders and PC gamers. For each numbered item below, assign exactly one category from this list:

- storage: SSDs, hard drives, RAM/memory, memory cards, USB drives
- display: monitors and screens
- component: CPU, GPU, motherboard, power supply, PC case, CPU cooler
- peripheral: keyboard, mouse, headset, webcam, microphone, controllers, other accessories
- game: video games and gaming consoles
- other: anything that doesn't fit the above

Respond with exactly one line per item, in the same order as the input, each line being a single category word from the list — nothing else. No numbering, no explanation, no extra text. The number of output lines must exactly match the number of input items."""

# Spec extraction — cleans up messy retail titles (Woot/Best Buy only;
# Steam game titles are already clean and don't have "specs" to extract)
# into a concise product name plus a few short technical specs, for the
# Discord embed and captions. See ai.spec_extraction.extract_clean_specs().
OPENROUTER_SPEC_EXTRACTION_MODEL = os.environ.get("OPENROUTER_SPEC_EXTRACTION_MODEL", "qwen/qwen3.7-flash")
OPENROUTER_SPEC_FALLBACK_MODEL = os.environ.get("OPENROUTER_SPEC_FALLBACK_MODEL", "google/gemini-2.5-flash-lite")
SPEC_EXTRACTION_SYSTEM_PROMPT = """You clean up messy retail product titles for a deal-finding bot focused on PC hardware and electronics. Given a raw title (and optional description), extract a clean, concise product name and up to 4 short technical specs.

Rules:
- Never invent a spec that isn't explicitly present or clearly implied in the input. If there is genuinely nothing worth calling out, return an empty specs list — do not pad it with anything invented.
- clean_title: the product name and model, stripped of SEO keyword clutter, under 100 characters.
- specs: 0 to 4 short strings (e.g. "Capacity: 2TB", "Interface: PCIe Gen4"), each under 60 characters.

Respond with only a JSON object in this exact shape: {"clean_title": string, "specs": [string, ...]}"""

# ---------------------------------------------------------------------------
# Woot feed selection and title/category filtering
# ---------------------------------------------------------------------------
# Woot feeds that map to your electronics/gaming focus.
# Valid options: All, Clearance, Computers, Electronics, Featured, Home,
# Gourmet, Shirts, Sports, Tools, Wootoff
WOOT_FEEDS = ["Electronics", "Computers"]

# Woot sometimes cross-lists a "featured" item across every feed regardless
# of category, so filtering by feed name alone isn't fully reliable. This
# catches anything with these words in the title and skips it. Add to this
# list as you spot more off-topic items sneaking through.
WOOT_EXCLUDE_KEYWORDS = [
    "squishmallow", "plush", "stuffed animal", "funko",
    "apparel", "shirt", "hoodie", "sneaker", "shoes",
    "cookware", "kitchen", "furniture", "decor", "bedding", "mattress",
]

# Allow-list for the PC-builds/monitors/gaming niche. A Woot deal must match
# at least one of these (in addition to clearing WOOT_EXCLUDE_KEYWORDS above)
# to post.
WOOT_INCLUDE_KEYWORDS = [
    "monitor", "display", "gpu", "graphics card", "video card",
    "motherboard", "cpu", "processor", "ram", "memory", "ssd", "nvme",
    "hard drive", "power supply", "psu", "pc case", "cpu cooler",
    "keyboard", "mouse", "mousepad", "headset", "webcam", "microphone",
    "laptop", "chromebook", "router", "gaming", "controller", "console",
]

# Woot's feed items carry a "Categories" field — a list of hierarchical
# strings like ["HOME", "TOOLS", "HOME/Lighting & Fans"]. This rejects
# whole off-topic departments by their top-level category (the part before
# the first "/").
WOOT_EXCLUDE_CATEGORIES = [
    "HOME", "TOOLS", "APPAREL", "TOYS", "SPORTS", "KITCHEN",
    "AUTOMOTIVE", "GOURMET", "BEAUTY", "PET",
]

# Best Buy keyword searches — narrowed to match the PC-builds/monitors focus.
BESTBUY_SEARCH_TERMS = [
    "monitor", "graphics card", "motherboard", "power supply",
    "pc case", "ssd", "ram memory", "cpu cooler",
    "mechanical keyboard", "gaming mouse", "gaming headset",
    "webcam", "gaming console", "video game",
]

# ---------------------------------------------------------------------------
# Deal-quality thresholds — tunable via .env without editing code.
# ---------------------------------------------------------------------------
MIN_DISCOUNT_PERCENT = int(os.environ.get("MIN_DISCOUNT_PERCENT", "20"))       # ignore anything below this % off
MIN_DOLLAR_SAVINGS = float(os.environ.get("MIN_DOLLAR_SAVINGS", "10"))         # AND ignore anything saving less than this in real dollars
SEEN_TTL_DAYS = 45              # forget deals older than this so the table doesn't grow forever

# Price-history quality gate. A deal needs at least this many DISTINCT
# CALENDAR DAYS of price_history observations before this gate applies at
# all — with no real history yet, everything falls back to the
# discount-vs-list-price check above.
PRICE_HISTORY_MIN_DAYS = int(os.environ.get("PRICE_HISTORY_MIN_DAYS", "3"))
# Once there's enough history, the sale price must be within this % of the
# lowest price ever recorded for that item to count as "near its floor."
PRICE_HISTORY_TOLERANCE_PERCENT = float(os.environ.get("PRICE_HISTORY_TOLERANCE_PERCENT", "5"))

# ---------------------------------------------------------------------------
# Derived / display constants
# ---------------------------------------------------------------------------
SOURCE_WEBHOOKS = {
    "Woot": WOOT_WEBHOOK_URL,
    "Best Buy": BESTBUY_WEBHOOK_URL,
    "Steam": STEAM_WEBHOOK_URL,
}

# Fixed display order for the digest's per-source fields — sources with
# nothing posted this run are simply left out.
DIGEST_SOURCE_ORDER = ["Woot", "Best Buy", "Steam"]

# Set True to show a large image instead of a small thumbnail — bigger and
# more eye-catching, but takes up more vertical space per post.
EMBED_USE_LARGE_IMAGE = False