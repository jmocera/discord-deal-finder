# deal-bot — Project Handoff

Last updated: 2026-08-21 — added the build-first reliability trio (shared
retry/backoff transport, watchdog heartbeat, phased pipeline with batched AI)
since the previous update; test suite is now 171 stdlib tests.

See also `VoltDrop_Project_Scope.md` in this repo for a narrative,
higher-level overview of the same project — this document stays the deep
technical/operational reference (exact schemas, commands, full bug list).

## What this is

An automated deal-finder bot for the electronics/PC-building/gaming niche. It
scrapes Woot, Best Buy, and Steam for discounted items, filters for quality
and relevance, and posts new deals to Discord (multiple channels, see below)
and Bluesky. It runs unattended on a GitHub Actions schedule — no server,
no local process to keep alive.

**Repo**: https://github.com/jmocera/discord-deal-finder (private, owner: jmocera)
**Local clone**: `C:\Users\johnm\Documents\deal-bot\`
**Entry point**: `python -m deal_bot` (the `deal_bot/` package; `__main__.py`
delegates to `deal_bot/pipeline.py:main`). There is no top-level `deal_bot.py`
anymore — a module file of that name would shadow the package on import.
Package layout: `config.py` (env/constants), `transport.py` (shared
retry/backoff HTTP seam), `sources/`, `storage/`, `integrations/`, `ai/`,
`pipeline.py` (the orchestrator), `weekly_digest.py`, and `watchdog.py`.

The now-deleted files from earlier iterations are superseded:
- `deal_bot.py` — the original monolith; split into the `deal_bot/` package.
- `deal_bot_dev.py` — an intermediate dev-iteration copy; fully superseded.

## How it runs

Three GitHub Actions workflows, all unattended:

- **`deal-bot`** (`.github/workflows/deal_bot.yml`): cron `0 */4 * * *` (every
  4 hours) plus `workflow_dispatch`. The main pipeline. GitHub does not
  guarantee exact schedule timing — delays of tens of minutes are normal.
- **`weekly-digest`** (`.github/workflows/weekly_digest.yml`): cron
  `0 12 * * 1` (Mondays at noon UTC) plus `workflow_dispatch` (with a
  `no_bluesky` boolean input for E2E testing). Runs `deal_bot.weekly_digest`.
- **`watchdog`** (`.github/workflows/watchdog.yml`): cron `0 * * * *`
  (every hour) plus `workflow_dispatch`. Dead-man's switch — see below.
- **`keepalive`** (`.github/workflows/keepalive.yml`): monthly empty commit to
  keep the schedule triggers from auto-disabling after 60 days of no commit
  activity. No manual attention needed.

**The posting pipeline** (`pipeline._process_deals`) is structured in explicit
phases (deterministic filter → batched AI enrichment → post loop → post-loop
reports) via a testable `_skip_reason` predicate, and spec extraction +
analysis run as ONE batched OpenRouter call per phase. This phase split is what
enables promoting the shadow classifier/scorer to real gates later.

**State**: 100% in Supabase (Postgres), nothing on local disk — required
because GitHub Actions runners are ephemeral (fresh filesystem every run).

## Reliability infrastructure (retry transport + watchdog)

- **`deal_bot/transport.py`** is the single HTTP seam for every outbound call
  (OpenRouter client, all Supabase storage, `run_log`, the weekly digest, and
  the watchdog). `transport.request(method, url, ...)` retries network errors
  and `{408,425,429,500,502,503,504}` with bounded backoff (default 3 attempts,
  ~1s, ~2s), honors `Retry-After` on 429 **capped at `MAX_SLEEP_SECONDS` (30)**,
  and never retries permanent 4xx. Callers use `from deal_bot import transport`
  + `transport.request(...)` (module-attribute access so tests can patch
  `deal_bot.transport.request`).
- **`load_seen` returns `dict | None`** — `None` on a hard fetch failure.
  `run_once()` treats that as fatal: it logs a clear `run_log` error and bails
  BEFORE fetching feeds, rather than running on an empty seen map (which would
  risk double-posting). `{}` still means "no Supabase config / nothing seen".
- **`deal_bot/watchdog.py` + hourly workflow** — the dead-man's switch. Every
  run writes a `run_log` row; the watchdog queries the most recent `ran_at` and
  posts a warning embed to `RUN_LOG_WEBHOOK_URL` if none lands within
  `max_hours` (default 6 = 2× the 4h cadence). This covers a **silently-skipped
  run** (already observed once pre-watchdog) or a crash before `log_run` — the
  failure the bot can't self-report. Exits 0 whether or not it alerts.
- **`log_run`** also routes through the transport, so a transient Supabase blip
  at log time doesn't silently drop the heartbeat row.

## Supabase

Project URL and service-role key are in `.env` (local) / the
`SUPABASE_URL` and `SUPABASE_SERVICE_KEY` GitHub secrets (remote). Access
is via the PostgREST REST API using `requests` — there is no SQL execution
tool available to Claude; any schema change (`ALTER TABLE`, etc.) has to be
handed to the user as SQL to run in Supabase's SQL editor.

**Tables:**

- `seen_deals` — dedupe state. `id` (text, pk, e.g. `"woot:<offerid>"`),
  `source`, `last_seen` (timestamptz), `sale_price`, `lowest_price`,
  `lowest_price_date`. Pruned automatically (`SEEN_TTL_DAYS = 45`).
- `price_history` — one row per deal per day (see "Bugs fixed" below for
  why it's per-*day*, not per-run). `id` (bigserial pk), `deal_id`,
  `source`, `observed_at` (timestamptz, default `now()`), `sale_price`,
  `list_price`, `discount_pct`, `observed_date` (date). Unique constraint
  `price_history_deal_id_observed_date_key` on `(deal_id, observed_date)`,
  upserted via `?on_conflict=deal_id,observed_date`. Rows older than the
  schema change (before `observed_date` existed) have `observed_date =
  NULL` — harmless, left as-is, backfill was attempted and abandoned (see
  below).
- `run_log` — one row per `run_once()` call, written whether the run
  succeeds or raises. `id`, `ran_at`, `deals_checked`, `posted`,
  `skipped_already_seen`, `skipped_no_better_price`,
  `skipped_below_threshold`, `skipped_not_near_historical_low`,
  `digest_sent` (bool), `error` (text, null on success).
- `posted_deals` — append-only log of every deal that actually posted
  (`id` text pk, `source`, `title`, `url`, `sale_price`, `list_price`,
  `posted_at` timestamptz default `now()`), written by
  `record_posted_deal()` after each successful post. Backs the weekly
  digest (`weekly_digest.py`). **Created** in the Supabase SQL editor; the
  `CREATE TABLE` statement also lives in `deal_bot/weekly_digest.py`.
  Pruned on each weekly-digest run (`prune_posted_deals`, 90-day TTL). If
  the table is missing, `record_posted_deal` fails silent but the digest's
  fetch treats the missing table as a hard failure (non-zero exit).

## Discord channels (7 core webhooks + 3 shadow-mode webhooks)

All currently point at **dev/test channels**, not production, on purpose —
decision was to validate everything there first, then flip those channels'
Discord *privacy setting* to make them the real production channels later
(and privatize the old production channels), rather than ever migrating
webhook URLs. So "dev" is likely to just become "prod" via a Discord
setting change, not a code change.

| Secret name | Purpose |
|---|---|
| `WOOT_WEBHOOK_URL` | Woot deal posts |
| `BESTBUY_WEBHOOK_URL` | Best Buy deal posts (dormant — no API key yet) |
| `STEAM_WEBHOOK_URL` | Steam deal posts |
| `DIGEST_WEBHOOK_URL` | End-of-run summary embed, only sent if `new_count > 0` |
| `RUN_LOG_WEBHOOK_URL` | Status embed **every** run, success or failure — the main "is it actually working" channel to check |
| `PRIVATE_WEBHOOK_URL` | Mirrors every posted deal as an embed + AI-written copy-paste caption (for manual X posting). This is the user's original pre-existing private channel, reused. |
| `SHADOW_CLASSIFIER_WEBHOOK_URL` | Reports the desirability classifier's KEEP/DROP judgments — observation only, doesn't affect real posting (see below) |
| `SHADOW_QUALITY_SCORER_WEBHOOK_URL` | Reports the deal quality scorer's 1-10 ratings — observation only, doesn't gate posting (set as a repo secret) |
| `SHADOW_CATEGORIZER_WEBHOOK_URL` | Reports the category tagger's per-deal classification — observation only, doesn't gate or route (set as a repo secret) |

## Bluesky

- Handle: `voltdrop.bsky.social`. App password in `BLUESKY_APP_PASSWORD` secret.
- Auto-posts the top `BLUESKY_MAX_POSTS_PER_RUN` (2) deals per run, ranked
  by **$ saved** (not discount %), among deals clearing
  `BLUESKY_MIN_DISCOUNT_PERCENT` (50%) — deliberately capped to avoid
  looking like a spam firehose on a new account.
- Posts use proper AT Protocol **link facets** (byte-offset annotations)
  so URLs render as clickable links — this was broken (posted as inert
  plain text) and fixed; two already-live broken posts were deleted and
  reposted with working links.
- Posts also carry a **rich link-preview card**: the deal's image is
  downloaded and uploaded as a blob via raw `com.atproto.repo.uploadBlob`,
  then attached as an `app.bsky.embed.external` card (title, description,
  thumbnail). Fails open at every step (no image, download error,
  non-image content-type, upload rejection all just mean no card, not a
  blocked post). See `_build_bluesky_embed()`.
- Every `#hashtag` in the caption gets its own clickable
  `app.bsky.richtext.facet#tag`, computed with the same UTF-8 byte-offset
  method as the link facet (`_build_tag_facets()`), verified to coexist
  correctly with the link facet (no overlaps, correct ordering) even with
  emoji/accented characters in the text.
- Both of the above are raw REST/XRPC calls, same as everything else in
  this file — the official `atproto` SDK was considered and explicitly
  declined to avoid adding a dependency used nowhere else in the project.

## OpenRouter / AI features

Account: user's own, currently dedicated entirely to this project (other
unrelated historical usage was from now-discontinued projects, confirmed).
Actual cost of everything built here so far: **~$0.0035 total**, verified
via OpenRouter's key-specific usage endpoint — realistically will never
need topping up at this design's usage level (cheap models, batching,
free-tier fallback).

- `OPENROUTER_API_KEY` — secret.
- `OPENROUTER_PRIMARY_MODEL` — variable, currently `deepseek/deepseek-v4-flash-0731` (paid, very cheap: $0.09/M prompt, $0.18/M completion tokens). Used for caption verdicts, the deal analyst, and the desirability classifier.
- `OPENROUTER_FALLBACK_MODEL` — variable, currently `nvidia/nemotron-3-ultra-550b-a55b:free` (caption/classifier/analyst free fallback).
- `OPENROUTER_SPEC_EXTRACTION_MODEL` — variable, currently `qwen/qwen3.7-flash` ($0.03/$0.13 per M).
- `OPENROUTER_SPEC_FALLBACK_MODEL` — variable, currently `google/gemini-2.5-flash-lite` (spec-extraction fallback, only called when the primary fails).
- `OPENROUTER_QUALITY_SCORER_MODEL` / `OPENROUTER_QUALITY_SCORER_FALLBACK_MODEL` — variables, `google/gemma-4-26b-a4b-it:free` and the paid `google/gemma-4-26b-a4b-it`. Used by the deal quality scorer.
- `OPENROUTER_CATEGORIZER_MODEL` / `OPENROUTER_CATEGORIZER_FALLBACK_MODEL` — same Gemma pair, used by the category tagger.
- `OPENROUTER_WEEKLY_DIGEST_MODEL` / `OPENROUTER_WEEKLY_DIGEST_FALLBACK_MODEL` — same Gemma pair, used by the weekly digest.

All of the above are Config Variables, not Secrets (see the Config reference below).
- **Reasoning-effort handling is model-specific, not a fixed rule** — this
  mattered in practice three separate times (see "Bugs fixed"). The
  caption/classifier models need `reasoning: {"effort": "low"}` explicitly
  set, or they can burn their whole token budget on internal reasoning and
  return null content. The spec-extraction model needs the *opposite* —
  setting any reasoning effort breaks it; omitting the parameter is what
  makes it reliable. `_call_openrouter()` takes `reasoning` as an explicit
  opt-in per call for exactly this reason — don't assume one config works
  for a model without testing it.

**Spec-extraction model history (2026-08-21):** switched from
`google/gemini-2.5-flash-lite` to `qwen/qwen3.7-flash` (3x cheaper, validated
against real titles). `xiaomi/mimo-v2.5` was tested as the fallback and
rejected — it is a verbose reasoning model that spent its entire token budget
on internal reasoning and returned null content on complex titles under every
config (`effort: low`, `enabled: false`, `max_tokens` up to 1200). Gemini was
kept as the fallback for that reason.

**New AI features (2026-08-21) — all shadow-mode or additive, none gate real
posts yet:**

- **Deal quality scorer** (`ai/deal_scorer.py`) — one batched call per run
  rating each posted deal 1-10 (free `google/gemma-4-26b-a4b-it:free`, paid
  Gemma fallback). Reports to `SHADOW_QUALITY_SCORER_WEBHOOK_URL`; would drop
  deals below `MIN_QUALITY_SCORE` (6) but is **shadow mode only**.
- **Deal analysis** (`ai/deal_analyst.py`) — a longer 2-3 sentence "why this
  is noteworthy" block rendered in the Discord embed's "Analysis" field
  (complements the short Bluesky caption). Same models as captions; fails
  open to an empty string. Live already.
- **Category tagger** (`ai/categorizer.py`) — one batched call tagging each
  posted deal into storage/display/component/peripheral/game/other. Reports
  to `SHADOW_CATEGORIZER_WEBHOOK_URL`; not yet used to gate or route.
- **Weekly digest** (`weekly_digest.py` + `.github/workflows/weekly_digest.yml`)
  — Mondays at noon UTC, reads the `posted_deals` table and has Gemma write a
  roundup posted to `DIGEST_WEBHOOK_URL` + Bluesky. Requires the `posted_deals`
  table (see Supabase section).

**Gemma reasoning finding:** the scorer/categorizer/weekly-digest Gemma models
need reasoning **omitted** (setting any effort burns their token budget) — the
opposite of the caption/classifier models. Confirmed empirically; locked in
by tests (`test_deal_scorer.py` / `test_categorizer.py`).

**Captions — upgraded from marketing copy to data-backed "verdicts"
(2026-08-16):** `build_ai_caption()` originally wrote generic engaging
captions; it now writes 1-2 sentence analytical verdicts explaining *why*
a deal is specifically noteworthy, grounded in real signals fed into the
prompt: the item's `clean_title`/`specs` (from spec extraction, below) and
real Supabase price-history context (`is_new_low` / `lowest_price`).
Anti-hallucination instruction forbids stating any spec not explicitly
given. Still a three-tier fallback: primary model → free fallback model →
plain template (`build_x_caption()`) — must never be able to block a
post. The exact prompt is in `deal_bot/config.py` around
`OPENROUTER_CAPTION_SYSTEM_PROMPT` / `deal_bot/ai/captions.py:build_ai_caption()`.

**Hashtags are deliberately *not* restricted to a fixed allow-list.** A
stricter spec proposed a hard 2-tag allow-list (`#gaming`/`#pcgaming`
only); this was explicitly reviewed and rejected in favor of keeping the
existing contextual, per-item hashtag variety (`#SSDDeals`,
`#BaldursGate3`, `#GamingMonitor`, etc.) — that variety was judged a real
differentiator worth keeping. `_hashtags_look_reasonable()` is a light
sanity check (≤4 tags, well-formed), not an allow-list. If this comes up
again, that's a considered decision, not an oversight.

**Spec extraction (2026-08-16, batched since 2026-08-21):**
`extract_clean_specs()` turns a messy retail title (e.g. `"Crucial P3 Plus 2TB
PCIe Gen4 3D NAND NVMe M.2 SSD, up to 5000MB/s - CT2000P3PSSD8"`) into a clean
product name plus 0-4 short technical specs, feeding both the Discord embed
(a "Specs" field) and the caption prompt above. **Woot/Best Buy only** — Steam
titles are already clean and don't have hardware specs to extract; gated on
`deal["source"] != "Steam"` in `_process_deals()`. Deliberately validates
0-4 specs, not a forced minimum — a genuinely low-info title should get an
honest empty list, not an invented one. Fails open per-field: an overlong
title falls back to the raw title independently of whether the specs in the
same response were otherwise valid, and vice versa.

Inside the phased pipeline it runs via **`extract_clean_specs_batch(titles)`** —
ONE batched OpenRouter call for all candidates instead of N per-deal calls —
with per-item validation identical to the per-deal function. If the batch
doesn't parse to a clean per-item list (wrong count, wrong shape), it falls
back to the per-deal `extract_clean_specs()` so a degraded batch is never worse
than the previous behavior. The per-deal version is kept for direct use/tests.

**Desirability classifier (SHADOW MODE ONLY, not gating
anything yet):** `classify_desirable_deals()` — one batched call per run,
judging every deal that *actually posted* this run as KEEP or DROP (would
a PC-building/gaming enthusiast genuinely want this, vs. generic/off-brand
noise that happened to clear the keyword/discount filters). Reports to
`SHADOW_CLASSIFIER_WEBHOOK_URL`. **Does not affect real posting at all
right now** — this is intentional. Fails open (keeps everything) if both
models fail, since a wrong DROP would be invisible (a good deal silently
never posted) while a wrong KEEP is just a visible, ignorable post.

The plan: watch the shadow channel against real deals over time, and only
promote this to an actual gate once its judgment is trusted. The prerequisite
restructure — turning `_process_deals()`'s loop into explicit phases so AI
judgment runs before posting — is **done** (the phased pipeline), so promotion
is now purely gated on reviewing enough shadow data (see BACKLOG.md §3).

Reasoning-effort quality tradeoff is only lightly validated: tested on
deliberately obvious cases (RTX 4070 Ti / Elden Ring / gaming mouse vs.
rubber bands / unbranded cable / mystery grab bag) — 100% correct, 4/4
consistent runs. **Not yet tested on genuinely ambiguous/borderline
items**, which is where low reasoning effort could plausibly matter more —
flagged as worth doing before fully trusting the classifier.

## Filtering / quality-gate logic (config in `deal_bot/config.py`, loop in `deal_bot/pipeline.py`)

- `MIN_DISCOUNT_PERCENT` (20), `MIN_DOLLAR_SAVINGS` (10) — basic thresholds.
- `WOOT_INCLUDE_KEYWORDS` / `WOOT_EXCLUDE_KEYWORDS` — title keyword allow/deny lists, Woot only.
- `WOOT_EXCLUDE_CATEGORIES` — Woot's `Categories` API field, top-level department exclusion (HOME, TOOLS, APPAREL, etc.) — coarser and less guessable than keywords alone.
- `PRICE_HISTORY_MIN_DAYS` (3) / `PRICE_HISTORY_TOLERANCE_PERCENT` (5) — a deal must be within 5% of its own recorded price floor, but only once ≥3 distinct days of `price_history` exist for that exact deal — dormant (no effect) until enough history accumulates. This exists because "% off list price" is a weak, gameable signal on its own.
- `BLUESKY_MIN_DISCOUNT_PERCENT` / `BLUESKY_MAX_POSTS_PER_RUN` — additional Bluesky-only gating, see above.

All of the above are GitHub **Variables** (not Secrets) — see the
Secrets-vs-Variables note below for why that distinction was deliberate.

## Config reference: Secrets vs. Variables

Real credentials → **Secrets** (write-only, correct for anything
sensitive): `WOOT_API_KEY`, `BESTBUY_API_KEY`, `SUPABASE_URL`,
`SUPABASE_SERVICE_KEY`, all 10 Discord webhook URLs (7 core + 3 shadow),
`BLUESKY_HANDLE`,
`BLUESKY_APP_PASSWORD`, `OPENROUTER_API_KEY`.

Plain tuning config → **Variables** (visible/editable later, correctly
*not* secret): `MIN_DISCOUNT_PERCENT`, `MIN_DOLLAR_SAVINGS`,
`BLUESKY_MIN_DISCOUNT_PERCENT`, `BLUESKY_MAX_POSTS_PER_RUN`,
`PRICE_HISTORY_MIN_DAYS`, `PRICE_HISTORY_TOLERANCE_PERCENT`,
`MIN_QUALITY_SCORE`, and the model-name overrides
(`OPENROUTER_PRIMARY_MODEL`, `OPENROUTER_FALLBACK_MODEL`,
`OPENROUTER_SPEC_EXTRACTION_MODEL`, `OPENROUTER_SPEC_FALLBACK_MODEL`,
`OPENROUTER_QUALITY_SCORER_MODEL`, `OPENROUTER_QUALITY_SCORER_FALLBACK_MODEL`,
`OPENROUTER_CATEGORIZER_MODEL`, `OPENROUTER_CATEGORIZER_FALLBACK_MODEL`,
`OPENROUTER_WEEKLY_DIGEST_MODEL`, `OPENROUTER_WEEKLY_DIGEST_FALLBACK_MODEL`).

These were **originally all put in Secrets** (including the tuning
numbers), which was wrong — Secrets can never be viewed again after
setting them, so there was no way to check "what's my discount threshold
set to right now?" six months later. Caught and fixed mid-project; worth
remembering this distinction for anything added later.

Local `.env` (gitignored, never committed — verified multiple times via
`git log --all --full-history -- .env`) mirrors all of the above for local
runs. `.env.example` is the committed, values-blank template — keep it in
sync when adding new config.

## Testing (added 2026-08-16, grown since)

Before this, the project had zero automated tests — every feature was
verified via live, real API calls during development instead (a deliberate
practice throughout, not a gap; kept doing this alongside the new tests, not
instead of them). Now there's a stdlib `unittest` suite, no new dependency:

- `tests/test_spec_extraction.py` — spec extraction (per-deal + the batched
  path), and via the real `_process_deals` code path the Steam-skip logic.
- `tests/test_deal_verdict.py` — caption "verdict" prompt context + fallbacks.
- `tests/test_amazon_vetting.py` — the standalone Amazon vetting tool.
- `tests/test_deal_scorer.py` / `tests/test_categorizer.py` — shadow AI
  features' parse + fail-open behavior.
- `tests/test_deal_analyst.py` / `tests/test_weekly_digest.py` — the analyst
  and the weekly digest (bulk fetch/prune/seed/clear, retry behavior, dry-run,
  exit codes).
- `tests/test_pipeline.py` — the phased pipeline: `_skip_reason` boundaries,
  `run_once`'s `load_seen`-None bail, batch spec/analysis fallbacks.
- `tests/test_watchdog.py` — the dead-man's switch: freshness, staleness,
  ordering of the `run_log` query.

**171 tests total**, run via `python -m unittest discover -s tests -p
"test_*.py"` (or `pytest tests/` if pytest happens to be installed locally —
it isn't by default, and isn't a project dependency).

Wired into `.github/workflows/deal_bot.yml` as a step named "Run Unit Tests,"
ahead of the actual bot execution. The bot-run step has `if: always()` so a
test failure shows up clearly (red step in the Actions log) but can **never
silently block the scheduled run** — this matters a lot for something meant to
run unattended; a broken test silently preventing all deal-finding for days,
with the only symptom being an absence of activity, is exactly the failure mode
`run_log`/`RUN_LOG_WEBHOOK_URL` (and now the watchdog) exist to prevent.

## Bugs found and fixed this session (context for future debugging)

1. **Best Buy's API query encoding** — `quote()` was applied to the whole
   query string including structural `&`/`=` characters, likely breaking
   the request. Never confirmed live since the Best Buy API key is still
   pending approval — **check this once that key comes through.**
2. **`run_log` reported all-zero counts on a mid-run crash** — the posting
   loop returned counts via a tuple that never completed if an exception
   hit partway through. Fixed by mutating a shared `stats` dict in place
   instead of returning a tuple at the end.
3. **N+1 Supabase queries** — price-history lookups were one live request
   per deal inside the posting loop (350+ per run). Replaced with
   `get_price_history_stats_bulk()`, a handful of chunked batch requests.
4. **Duplicate `price_history` rows** — Woot's Electronics/Computers feeds
   (and potentially Best Buy's overlapping search terms once live) can
   list the same `deal_id` twice within one run, with no dedup before
   `record_price_observations()`. Fixed two ways: (a) `all_deals` is now
   deduped by id right after fetching, (b) the insert became an upsert
   keyed on `(deal_id, observed_date)`, needing the schema change
   described above.
5. **Bluesky posts weren't clickable** — AT Protocol requires explicit
   link "facets" (byte-offset annotations); it does not auto-linkify plain
   URLs the way most social platforms do. Fixed with UTF-8 byte-offset
   facet computation (character offsets break with multi-byte characters
   like em dashes). Also fixed a related bug where long-caption truncation
   could clip the URL entirely.
6. **Workflow file silently not registering with GitHub Actions** — an
   unquoted `on:` key is ambiguous with YAML 1.1's boolean `on`/`off`
   keywords; quoting it as `"on":` fixed it. If a workflow file ever seems
   to just not show up in `gh workflow list`, check this first.
7. **AI calls returning null content** — both OpenRouter models are
   reasoning models that can burn their entire token budget on internal
   reasoning and return nothing. Fixed with `reasoning: {"effort": "low"}`
   plus generous `max_tokens` floors — different floors needed for
   captions (350) vs. classification (`300 + 15/item`, capped at 1500);
   the classification task needed more headroom than captions did.
8. **The spec-extraction model needed the opposite reasoning fix from #7**
   — for `google/gemini-2.5-flash-lite`, setting *any* explicit reasoning
   effort (even "low") reliably burned the whole token budget and
   returned truncated garbage instead of JSON; *omitting* the parameter
   entirely was what made it reliable. This is why `_call_openrouter()`
   takes `reasoning` as an explicit opt-in per call rather than a fixed
   default — don't assume the #7 fix generalizes to a new model without
   testing it.
9. **The caption "verdict" upgrade (2026-08-16) hit a truncation bug at
   the old token budget** — the new prompt ("explain *why* this is
   noteworthy") is a more demanding task than the old "write an engaging
   caption," and needed more headroom even at low reasoning effort (350 →
   600 `max_tokens`) or it would cut off mid-sentence. Confirmed reliable
   across 9 repeated real-API test calls after the fix, with no
   hallucinated specs in any of them.
10. **PostgREST rejects an unbounded DELETE (2026-08-21)** — the weekly
    digest's `--clear` initially did `DELETE` with no WHERE clause and got
    HTTP 400. Fixed by scoping to seed rows only (`id` `like 'seed:%'`),
    so testing cleanup can never wipe the whole `posted_deals` table.
11. **`load_seen` no-op-on-failure was a silent double-post risk
    (2026-08-21)** — a transient Supabase failure returned `{}`, so the run
    treated every deal as new. `load_seen` now returns `None` on a hard
    failure and `run_once` bails with a logged error before fetching feeds.
12. **A test's broken config restore cascaded into later modules
    (2026-08-21)** — a `SkipReasonTests` tearDown referenced attributes
    never saved by setUp, raising in tearDown and leaving mutated
    thresholds that leaked into subsequent test files. Fixed by saving all
    four thresholds and pinning a deterministic baseline per test.
13. **The free-tier Gemma models intermittently 429** (observed
    2026-08-21) — the designated recovery is the paid
    `google/gemma-4-26b-a4b-it` fallback, wired as the fallback model in
    every Gemma-based feature.

## Design principles established (worth preserving)

- **Verify against real data before declaring anything done** — this
  project leans heavily on test scripts that hit real Supabase/Discord/
  Bluesky/OpenRouter endpoints (safely — using test channels, cleanup
  after, no spam) rather than trusting code review alone.
- **AI features fail open, never fail closed** — a wrong permissive
  action (an extra caption, a kept-but-mediocre deal) is recoverable and
  visible; a wrong suppressive action (a dropped deal, a blocked post) is
  invisible and worse. Every AI integration here defaults to "if anything
  goes wrong, behave as if the AI wasn't there."
- **New/risky automated-judgment features get a shadow/observation period**
  before being trusted to actually gate behavior (see the classifier).
- **Secrets vs. Variables discipline** — credentials only in Secrets,
  everything else in Variables.
- Commit messages explain *why*, not just *what*; one logical change per
  commit; compile-check and live-verify before and after every push.
- **Don't assume a reasoning-effort fix generalizes to a new model** — it
  didn't, twice (bugs #7 vs #8, and #7 vs #9). Test empirically per model
  and per task, every time.
- **Prefer real model-chosen variety over a fixed vocabulary when the
  variety itself has been shown to add value** — the hashtag allow-list
  question (declined) is the concrete example; don't reintroduce a fixed
  list here without re-litigating that decision deliberately.
- **A new automated test suite should never be able to silently block the
  thing it's protecting** — see the Testing section's `if: always()` note.

## Open items / next steps

- **Best Buy API key** — still pending approval as of last check. Once it
  arrives: set `BESTBUY_API_KEY`, and specifically double-check bug #1 above
  (the query-encoding issue) since it's never been exercised against a real
  key.
- **Shadow-feature promotion** — the classifier/scorer/categorizer are shadow
  mode until enough real-world runs are reviewed (the pipeline's phase split
  that promotion needs is already done). See BACKLOG.md §3.
- **Webhook false-negative dedupe gap** — if a Discord webhook call actually
  succeeds server-side but the HTTP response is lost before the code sees it,
  `seen_deals` never gets updated for that deal, and since state persists
  across runs, it could look "never posted" on a *future* run and genuinely
  post twice. Rare (needs a network failure at exactly the wrong moment), real,
  not fixed.
- **`price_history` growth / `load_seen` pagination** — `price_history` grows a
  row per deal per day forever, and `load_seen` doesn't page past PostgREST's
  ~1000-row cap. Fine at current volume with TTL pruning, but worth revisiting
  before Best Buy more than doubles deal count.
- **Dev → prod channel flip** — whenever ready, this is a Discord privacy
  setting change on the existing channels, not a code change.
- Old `price_history` rows retain `observed_date = NULL` (pre-dates the
  schema fix) — harmless, a real backfill would need to also collapse the
  historical duplicate rows first, deliberately not done.

## Useful commands

```bash
# Run the test suite
python -m unittest discover -s tests -p "test_*.py"
# or one module: python -m unittest tests.test_pipeline -v

# Manually trigger a run / the weekly digest / the watchdog
gh workflow run deal_bot.yml --repo jmocera/discord-deal-finder
gh workflow run weekly_digest.yml --repo jmocera/discord-deal-finder -f no_bluesky=true   # E2E: skip Bluesky
gh workflow run watchdog.yml --repo jmocera/discord-deal-finder

# Weekly digest CLI (local E2E), see deal_bot/weekly_digest.py --help
python -m deal_bot.weekly_digest --dry-run    # fetch + build, post nothing
python -m deal_bot.weekly_digest --seed 7     # insert fake rows (then --clear)
python -m deal_bot.weekly_digest --clear
python -m deal_bot.watchdog                   # watchdog CLI: --max-hours N

# Recent run history
gh run list --repo jmocera/discord-deal-finder --workflow=deal_bot.yml --limit 10

# See a specific run's output
gh run view <run-id> --repo jmocera/discord-deal-finder --log

# What's configured (values hidden for secrets, visible for variables)
gh secret list --repo jmocera/discord-deal-finder
gh variable list --repo jmocera/discord-deal-finder
```

Querying Supabase or OpenRouter directly for debugging: import the `deal_bot`
package (`sys.path.insert(0, r"C:\Users\johnm\Documents\deal-bot")`) and
reuse `deal_bot.storage.supabase._supabase_headers()`, `deal_bot.config.SUPABASE_URL`,
`deal_bot.config.OPENROUTER_API_KEY`, etc. rather than re-deriving connection
details — this is the pattern used throughout this project's own test scripts.
