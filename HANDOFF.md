# deal-bot — Project Handoff

Last updated: 2026-08-09 (during initial build-out session)

## What this is

An automated deal-finder bot for the electronics/PC-building/gaming niche. It
scrapes Woot, Best Buy, and Steam for discounted items, filters for quality
and relevance, and posts new deals to Discord (multiple channels, see below)
and Bluesky. It runs unattended on a GitHub Actions schedule — no server,
no local process to keep alive.

**Repo**: https://github.com/jmocera/discord-deal-finder (private, owner: jmocera)
**Local clone**: `C:\Users\johnm\Documents\deal-bot\`
**Entry point**: `deal_bot.py` — a single file, deliberately not split into
modules yet; read its own module docstring for a condensed version of this
same context.

Two other files in `C:\Users\johnm\Documents\` are **not** part of this
project and can be ignored/are superseded:
- `deal_bot.py` — the original, very first version, never modified after
  this project's `deal-bot/deal_bot.py` was created. Not deployed anywhere.
- `deal_bot_dev.py` — an intermediate dev-iteration copy used for local
  experimentation before this repo existed. Fully superseded.

## How it runs

- **Scheduler**: GitHub Actions, `.github/workflows/deal_bot.yml`, cron
  `0 */4 * * *` (every 4 hours) plus `workflow_dispatch` for manual runs.
  GitHub does not guarantee exact schedule timing — delays of tens of
  minutes are normal. One run was observed to be silently skipped
  entirely (not just delayed) with no clear root cause found (no GitHub
  incident, repo/billing looked fine); worth watching if it recurs.
- **Keepalive**: `.github/workflows/keepalive.yml` runs monthly and pushes
  an empty commit. This exists because GitHub auto-disables a workflow's
  `schedule` trigger after 60 days of *no git commit activity* on the repo
  (workflow runs themselves don't count) — this keeps that from ever
  silently lapsing. No manual attention needed.
- **State**: 100% in Supabase (Postgres), nothing on local disk — required
  because GitHub Actions runners are ephemeral (fresh filesystem every run).

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

## Discord channels (7 webhooks)

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

## OpenRouter / AI features

Account: user's own, currently dedicated entirely to this project (other
unrelated historical usage was from now-discontinued projects, confirmed).
Actual cost of everything built here so far: **~$0.0035 total**, verified
via OpenRouter's key-specific usage endpoint — realistically will never
need topping up at this design's usage level (cheap models, batching,
free-tier fallback).

- `OPENROUTER_API_KEY` — secret.
- `OPENROUTER_PRIMARY_MODEL` — variable, currently `deepseek/deepseek-v4-flash-0731` (paid, very cheap: $0.09/M prompt, $0.18/M completion tokens).
- `OPENROUTER_FALLBACK_MODEL` — variable, currently `openai/gpt-oss-20b:free`.
- **Both are reasoning models** — this mattered a lot in practice (see
  "Bugs fixed"). Calls use `reasoning: {"effort": "low"}` plus a generous
  `max_tokens` floor; too tight a budget lets reasoning consume the whole
  thing and return null content instead of an answer.

**Feature 1 — AI-written captions (LIVE, actually affects what posts):**
`build_ai_caption()` replaces the old mechanical caption template for both
the Bluesky auto-post and the private-channel mirror. Three-tier fallback:
primary model → free fallback model → plain template (`build_x_caption()`)
— must never be able to block a post. The exact prompt (system + how the
per-deal user prompt is built) is in `deal_bot.py` around
`OPENROUTER_CAPTION_SYSTEM_PROMPT` / `build_ai_caption()`.

**Feature 2 — Desirability classifier (SHADOW MODE ONLY, not gating
anything yet):** `classify_desirable_deals()` — one batched call per run,
judging every deal that *actually posted* this run as KEEP or DROP (would
a PC-building/gaming enthusiast genuinely want this, vs. generic/off-brand
noise that happened to clear the keyword/discount filters). Reports to
`SHADOW_CLASSIFIER_WEBHOOK_URL`. **Does not affect real posting at all
right now** — this is intentional. Fails open (keeps everything) if both
models fail, since a wrong DROP would be invisible (a good deal silently
never posted) while a wrong KEEP is just a visible, ignorable post.

The plan: watch the shadow channel against real deals over time, and only
promote this to an actual gate once its judgment is trusted. Turning it
into a real gate requires restructuring `_process_deals()`'s loop into two
phases (collect candidates → one batched classify call → post the KEEPs) —
not yet done, since shadow mode doesn't need that restructuring.

Reasoning-effort quality tradeoff is only lightly validated: tested on
deliberately obvious cases (RTX 4070 Ti / Elden Ring / gaming mouse vs.
rubber bands / unbranded cable / mystery grab bag) — 100% correct, 4/4
consistent runs. **Not yet tested on genuinely ambiguous/borderline
items**, which is where low reasoning effort could plausibly matter more —
flagged as worth doing before fully trusting the classifier.

## Filtering / quality-gate logic (all in `deal_bot.py`)

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
`SUPABASE_SERVICE_KEY`, all 7 Discord webhook URLs, `BLUESKY_HANDLE`,
`BLUESKY_APP_PASSWORD`, `OPENROUTER_API_KEY`.

Plain tuning config → **Variables** (visible/editable later, correctly
*not* secret): `MIN_DISCOUNT_PERCENT`, `MIN_DOLLAR_SAVINGS`,
`BLUESKY_MIN_DISCOUNT_PERCENT`, `BLUESKY_MAX_POSTS_PER_RUN`,
`PRICE_HISTORY_MIN_DAYS`, `PRICE_HISTORY_TOLERANCE_PERCENT`,
`OPENROUTER_PRIMARY_MODEL`, `OPENROUTER_FALLBACK_MODEL`.

These were **originally all put in Secrets** (including the tuning
numbers), which was wrong — Secrets can never be viewed again after
setting them, so there was no way to check "what's my discount threshold
set to right now?" six months later. Caught and fixed mid-project; worth
remembering this distinction for anything added later.

Local `.env` (gitignored, never committed — verified multiple times via
`git log --all --full-history -- .env`) mirrors all of the above for local
runs. `.env.example` is the committed, values-blank template — keep it in
sync when adding new config.

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

## Open items / next steps

- **Best Buy API key** — still pending approval as of last check (applied
  ~5+ days prior). Once it arrives: set `BESTBUY_API_KEY`, and specifically
  double-check bug #1 above (the query-encoding issue) since it's never
  been exercised against a real key.
- **Shadow classifier** — needs more real-world runs before deciding
  whether to promote it to an actual gate. Also worth testing reasoning
  effort (`low` vs `medium`) specifically on ambiguous/borderline items
  before trusting it on those.
- **Scheduled-run reliability** — one 00:00 UTC anchor was silently
  skipped by GitHub with no root cause found; a one-time check was
  scheduled for the following anchor via `mcp__scheduled-tasks` but its
  outcome isn't confirmed in this conversation — worth checking recent
  `gh run list` history for a pattern.
- **Dev → prod channel flip** — whenever ready, this is a Discord privacy
  setting change on the existing channels, not a code change.
- Old `price_history` rows retain `observed_date = NULL` (pre-dates the
  schema fix) — harmless, a real backfill would need to also collapse the
  historical duplicate rows first, deliberately not done.

## Useful commands

```bash
# Manually trigger a run
gh workflow run deal_bot.yml --repo jmocera/discord-deal-finder

# Recent run history
gh run list --repo jmocera/discord-deal-finder --workflow=deal_bot.yml --limit 10

# See a specific run's output
gh run view <run-id> --repo jmocera/discord-deal-finder --log

# What's configured (values hidden for secrets, visible for variables)
gh secret list --repo jmocera/discord-deal-finder
gh variable list --repo jmocera/discord-deal-finder
```

Querying Supabase or OpenRouter directly for debugging: import `deal_bot`
as a module (`sys.path.insert(0, r"C:\Users\johnm\Documents\deal-bot")`)
and reuse its `_supabase_headers()` / `SUPABASE_URL` / `OPENROUTER_API_KEY`
etc. rather than re-deriving connection details — this is the pattern used
throughout this project's own test scripts.
