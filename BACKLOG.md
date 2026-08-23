# VoltDrop — Backlog & Roadmap

Companion to `HANDOFF.md`. HANDOFF is the operational reference (exact schemas,
commands, bug list); this is the forward-looking roadmap — what's shipped, what's
in flight, what's waiting, and why. Items are tracked by status so nothing sits
silently.

---

## 1. Purpose

Track every planned, in-progress, blocked, or done piece of work so the project
keeps moving without relitigating closed decisions. Each item has a status, an
owner, and an explicit trigger to start work.

Status legend: `done` / `in-progress` / `blocked` / `backlog`.

---

## 2. Shipped

- **Structural refactor** — `deal_bot.py` monolith → `deal_bot/` package
  (`config`, `transport`, `sources/`, `storage/`, `integrations/`, `ai/`,
  `pipeline`, `weekly_digest`, `watchdog`, `__main__`). Entry point
  `python -m deal_bot`. Commit `c71ad09`.
- **Model cost reduction** — spec extraction → `qwen/qwen3.7-flash` (~3x
  cheaper); caption/classifier fallback → `nvidia/nemotron-3-ultra-550b-a55b:free`.
- **Deal analysis** (live) — `ai/deal_analyst.py`, "Analysis" field on Discord embeds.
- **Deal quality scorer** (shadow) — `ai/deal_scorer.py`, 1-10 ratings.
- **Category tagger** (shadow) — `ai/categorizer.py`, 6 categories.
- **Weekly digest** — `weekly_digest.py` + `weekly_digest.yml`.
- **Shared retry/backoff transport** — `deal_bot/transport.py`, the single HTTP seam with bounded retry + `Retry-After` cap (commit `29712fa`, review-hardened in `6720615`).
- **Watchdog heartbeat** — `watchdog.py` + hourly `watchdog.yml` (dead-man's switch).
- **Phased pipeline + batched AI** — `_process_deals` split into explicit phases with a testable `_skip_reason`; spec + analysis run one batched call per phase.
- **Code-review hardening pass (2026-08-22)** — six bug fixes (Discord non-JSON-429 crash, shadow-embed field overflow, Best Buy query encoding, Bluesky login-shape guard, watchdog false alarm on missing config, stale `posted_at` on re-post); source GETs moved onto the shared retry transport; call-time config binding (`post_len.hard_target()`, categorizer regex); new `deal_bot/display.py` shared price/discount formatting; strict new-low semantics with tie-keeps-date. See HANDOFF.md bugs #14–#20.
- **277 stdlib tests**, all passing.

---

## 3. Shadow-feature promotion (A — waiting on data)

**Status:** blocked on data · **Owner:** user

The quality scorer, desirability classifier, and category tagger are all
observation-only — they report to shadow Discord channels but do not gate or
route any posts. This is deliberate (fail-open discipline: a wrong DROP is an
invisible lost deal).

- **Trigger to promote:** review 5-7 real shadow runs and agree the
  KEEP/DROP + 1-10 scores + categories look sane against actual deals.
- **Rollout once trusted:** two-phase — (1) shadow-report continues while a
  dry-run gate simulates what would have been filtered, (2) promote to a real
  gate by wiring the AI verdict into the deterministic-filter phase
  (`_skip_reason` already runs before posting).
- **Prerequisite shipped:** the phased `_process_deals()` that promotion needs
  is done (candidates collected → batched AI → post), so this item is now
  purely data-gated, not engineering-gated.
- **Not yet promoted** — the category tagger has no consumer yet (see §8 channel
  routing), so it should probably stay shadow until routing lands.

---

## 4. Weekly digest validation + hardening

**Status:** in-progress · **Owner:** user

- **E2E test pass** — verified end-to-end: seed fake `posted_deals` rows →
  `--dry-run` (fetch + build, no post) → live Discord post (Bluesky
  intentionally skipped) → `workflow_dispatch` the real `weekly_digest.yml` →
  clear seeded rows. Live run surfaced and fixed the unbounded-DELETE issue.
- **Pruning** — done: `prune_posted_deals()` deletes rows older than 90 days
  on each normal run, so the append-only `posted_deals` table stays bounded.
- **Retry + exit-code hardening** — done: the digest's Supabase calls now go
  through a shared `_supabase_request` helper (3 attempts, backoff, honors
  Retry-After, no retry on permanent 4xx), and `main()` exits non-zero when
  nothing was delivered (or the fetch/table failed) so a real Monday failure
  turns the workflow red. A missing `posted_deals` table or non-200 fetch is
  now a hard failure (was a silent skip); a week with no posted deals is
  still a healthy exit 0.
- **Per-run AI budget** — the digest makes one Gemma call; fine today, but if
  deal volume climbs, watch `timeout-minutes: 5` headroom.

---

## 5. Best Buy source activation

**Status:** blocked · **Owner:** user

- Best Buy API key still pending approval.
- The `quote()` query-encoding bug is now fixed and locked by regression
  tests (`tests/test_sources.py`); still never exercised against a real
  key — do one live verification when it arrives.
- Re-check the key-redaction fix (`_redact()`) doesn't leak the key anywhere
  once real traffic flows.

---

## 6. Monetization

**Status:** backlog · **Owner:** user

- No affiliate tagging or `#ad` disclosure exists on the automated pipeline's
  outputs (Woot/Best Buy/Steam) — only manual Amazon posts carry disclosure.
- **Trigger:** CJ Affiliate (Woot) and/or Impact.com (Best Buy/Walmart)
  approval. Then wire tags into the `deal_bot` package and add FTC `#ad` to
  auto-posts.
- `vet_amazon_deal.py` (manual Amazon assistant) exists but isn't a *required*
  step — adoption is a process/discipline question, not a code one.

---

## 7. Reliability

**Status:** in-progress · **Owner:** user

- **Shared retry/backoff transport** — done: `deal_bot/transport.py` is the single seam for every outbound HTTP call that is safe to auto-retry (OpenRouter client, all Supabase storage, run_log, weekly digest, watchdog, and since the 2026-08-22 hardening pass the read-only Woot/Best Buy/Steam GETs). Discord webhook POSTs and Bluesky XRPC posts are deliberately excluded — non-idempotent POSTs must never be auto-retried (a lost response + retry = double-posted deal). Bounded retries on network errors + `{408,425,429,500,502,503,504}`, honors Retry-After, no retry on permanent 4xx. Closes the double-post risk from a transient `load_seen` failure (which now returns `None` — `run_once` bails and logs rather than running on an empty seen map).
- **Watchdog heartbeat (dead-man's switch)** — done: `deal_bot/watchdog.py` +
  `.github/workflows/watchdog.yml` (hourly). Alerts if no `run_log` row lands
  within `max_hours` (default 6 = 2x the 4h cadence), covering the silently-
  skipped-run failure the bot can't self-report. Live-verified: fresh run →
  no alert.
- **Two-phase pipeline + batched AI enrichment** — done: `_process_deals` is
  now explicit phases (deterministic filter via testable `_skip_reason` →
  batched spec+analysis → post loop → bluesky/digest/shadow reports), and
  spec extraction + analysis run as ONE batched call per phase instead of
  N sequential calls (with per-item fallback to the old per-deal path on a
  degraded batch). This is the enabling restructure for gate promotion.
- **Webhook false-negative dedupe gap** — if a Discord webhook call succeeds
  server-side but the HTTP response is lost, `seen_deals` never updates and the
  deal could post twice on a later run. Rare, real, not fixed (documented in
  HANDOFF's bug list).
- **One silently-skipped scheduled run** — now surfaced by the watchdog rather
  than left to "absence of activity"; root cause still unknown but the failure
  is no longer silent.

---

## 8. Idea backlog (unsized)

**Status:** backlog · **Owner:** user

- **Category-based channel routing** — the category tagger already produces
  storage/display/component/peripheral/game/other; use it to route posts to
  per-category Discord channels (or better hashtag/analysis targeting).
- **Price-prediction buy-now-or-wait** — use the accumulated `price_history`
  to signal whether a price is at a real floor or likely to drop further.
- **User-facing Discord deal queries** — e.g. "any good GPU deals?" → curated
  AI response from Supabase history.
- **Monthly digest** — a longer-form recap on top of the weekly one.
- **Richer competitive context in the analyst** — feed competitor-price data so
  the analysis can say "beats comparable X at this price."

---

## 9. Decision log

Closed decisions worth recording so they aren't relitigated.

- **`xiaomi/mimo-v2.5` rejected as spec fallback** — it is a verbose reasoning
  model that spends its entire token budget on internal reasoning and returned
  null content on complex titles under every config (`effort: low`,
  `enabled: false`, `max_tokens` up to 1200). `google/gemini-2.5-flash-lite`
  kept as the fallback instead.
- **Gemma models need reasoning OMITTED** — the scorer/categorizer/digest Gemma
  models burn their token budget when any reasoning effort is set (the opposite
  of the caption/classifier models which need `{"effort": "low"}`). Confirmed
  empirically; locked in by `test_deal_scorer.py` / `test_categorizer.py`.
- **Free-tier Gemma is rate-limited** — `google/gemma-4-26b-a4b-it:free`
  intermittently 429s; the paid `google/gemma-4-26b-a4b-it` fallback is the
  designed recovery path.
- **Shadow mode before real gates** — new/risky automated-judgment features
  always get an observation period before being trusted to gate posts.