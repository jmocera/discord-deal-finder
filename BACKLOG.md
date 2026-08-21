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
  (`config`, `sources/`, `storage/`, `integrations/`, `ai/`, `pipeline`,
  `__main__`). Entry point `python -m deal_bot`. Commit `c71ad09`.
- **Model cost reduction** — spec extraction → `qwen/qwen3.7-flash` (~3x
  cheaper); caption/classifier fallback → `nvidia/nemotron-3-ultra-550b-a55b:free`.
- **Deal analysis** (live) — `ai/deal_analyst.py`, "Analysis" field on Discord embeds.
- **Deal quality scorer** (shadow) — `ai/deal_scorer.py`, 1-10 ratings.
- **Category tagger** (shadow) — `ai/categorizer.py`, 6 categories.
- **Weekly digest** — `weekly_digest.py` + `weekly_digest.yml`.
- **119 stdlib tests**, all passing.

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
  gate.
- **Engineering note:** promoting requires restructuring `_process_deals()` in
  `pipeline.py` into two phases (collect candidates → one batched AI call →
  post the survivors), which shadow mode deliberately avoids today.
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
- Once it arrives: verify the long-dormant `quote()` query-encoding logic in
  `sources/bestbuy.py` against a real key (it has never been exercised live).
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

**Status:** backlog · **Owner:** user

- **Webhook false-negative dedupe gap** — if a Discord webhook call succeeds
  server-side but the HTTP response is lost, `seen_deals` never updates and the
  deal could post twice on a later run. Rare, real, not fixed (documented in
  HANDOFF's bug list).
- **One silently-skipped scheduled run** — a single GitHub schedule run was
  skipped with no root cause. Watch for a pattern, not a one-off.
- **Per-run AI latency** — the 2-model spec-extraction chain adds worst-case
  latency per deal; bounded today, worth a per-run budget if volume climbs.

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