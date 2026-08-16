# VoltDrop — Project Scope

**Scope note:** this document covers the automated `deal_bot.py` pipeline specifically — everything in this document is drawn directly from verified work in this session (live API tests, real GitHub Actions runs, actual git history), not from external notes or unverified claims. Business-development topics (affiliate programs, manual curation workflows, brand/growth strategy) may exist elsewhere but are outside what this document can vouch for, and are deliberately not included here. For deep technical reference (exact schemas, commands, full bug list), see `HANDOFF.md` in this repo — this document is the higher-level narrative; that one is the operational reference.

**Repo:** https://github.com/jmocera/discord-deal-finder (private)

---

## 1. System overview

VoltDrop's automated side is a single Python script (`deal_bot.py`) that runs unattended on a GitHub Actions schedule (every 4 hours), with no server and no persistent local process:

```
 Woot API    Best Buy API    Steam API
    │             │              │
    └─────────────┼──────────────┘
                   ▼
            deal_bot.py (GitHub Actions, cron every 4h)
                   │
   ┌───────────────┼────────────────────┬─────────────────┐
   ▼               ▼                    ▼                 ▼
Supabase      Discord webhooks (7)   Bluesky (AT       OpenRouter
(seen_deals,  Woot / Best Buy /      Protocol, raw     (3 features —
price_history, Steam / Digest /      REST, no SDK)     see §6)
run_log)      Private queue /
              Run Log / Shadow
              Classifier
```

Best Buy is currently dormant in the live pipeline — its API key is still pending approval as of this writing, so that source contributes zero deals until it's granted.

---

## 2. Data sources & filtering

| Source | Access | Status |
|---|---|---|
| Woot API | Free key | Live — two feeds (Electronics, Computers) |
| Best Buy Products API | Free key | **Pending approval** — code is ready, key isn't |
| Steam featured/specials | Free, no key | Live — curated "Specials" only |

Filtering, in order: `WOOT_INCLUDE_KEYWORDS`/`WOOT_EXCLUDE_KEYWORDS` (Woot only) → `WOOT_EXCLUDE_CATEGORIES` (Woot's own `Categories` field, top-level department exclusion) → `MIN_DISCOUNT_PERCENT` (20%) **and** `MIN_DOLLAR_SAVINGS` ($10), both required → the price-history quality gate (below).

**Price-history quality gate:** once `PRICE_HISTORY_MIN_DAYS` (3) of distinct-day history exists for an exact item, its sale price must be within `PRICE_HISTORY_TOLERANCE_PERCENT` (5%) of its own recorded low. This exists because "% off list price" alone is a weak, gameable signal — list prices can be inflated by the retailer; a price's relationship to its *own* history is harder to fake.

---

## 3. Data persistence (Supabase)

Chosen specifically because GitHub Actions runners are ephemeral — a local JSON file for dedupe state would never survive between scheduled runs. Three tables:

- **`seen_deals`** — dedupe state, keyed by deal ID (`source:id`), tracks last-seen price so a further price drop can re-trigger a post.
- **`price_history`** — one row per deal *per day* (not per run — see §5 for why that distinction mattered), upserted on `(deal_id, observed_date)`.
- **`run_log`** — one row per run, success or failure, with full counts — written even when the run crashes partway through, and mirrored live to a dedicated Discord channel (`RUN_LOG_WEBHOOK_URL`) so a failure is never silent.

---

## 4. Deployment

- **GitHub Actions** (`.github/workflows/deal_bot.yml`): `cron: "0 */4 * * *"`, `workflow_dispatch` for manual runs, Python 3.13, 15-minute timeout.
- **`keepalive.yml`**: a separate workflow, runs monthly, pushes an empty commit — GitHub auto-disables a workflow's *schedule* trigger after 60 days of no git commit activity (workflow runs don't count toward that), so this exists purely to keep the schedule itself from silently lapsing.
- **CI test step** (added today, see §7): runs the unit test suite before the bot executes. Deliberately wired with `if: always()` on the bot-execution step, so a test regression is visible (shows as a distinct failed step) but can never silently prevent the actual scheduled run — consistent with everything else built here to make failures loud, not silent.
- **Secrets vs. Variables**: real credentials (API keys, webhook URLs, `SUPABASE_SERVICE_KEY`, `BLUESKY_APP_PASSWORD`, `OPENROUTER_API_KEY`) are GitHub *Secrets*. Plain tuning numbers and model names (`MIN_DISCOUNT_PERCENT`, `OPENROUTER_PRIMARY_MODEL`, etc.) are GitHub *Variables* — this split was corrected mid-project after the tuning values were initially, incorrectly stored as write-only Secrets.
- **`.env` was never committed to git** — verified via three independent methods (`git log --all --full-history`, `git ls-files`, a direct GitHub API 404 on the file path).

---

## 5. Known bugs found and fixed (this pipeline, verified)

1. **`run_log` reported all-zero counts on a mid-run crash** — fixed by mutating a shared `stats` dict in place rather than returning a tuple that could go uncompleted.
2. **N+1 Supabase queries for price-history lookups** (one live request per deal) — replaced with a batched, chunked lookup.
3. **Duplicate `price_history` rows** — root cause was Woot's Electronics/Computers feeds (and potentially Best Buy's overlapping search terms) surfacing the same item twice within one run, with no dedup before it reached the database. Fixed two ways: `all_deals` is deduplicated by ID immediately after fetching, and the insert became an upsert keyed on `(deal_id, observed_date)`. Verified against production data post-fix — zero duplicate same-day rows across multiple real scheduled runs.
4. **Bluesky posts weren't clickable** — AT Protocol requires explicit byte-offset "facets" for links; it doesn't auto-linkify plain URLs the way most platforms do. Fixed, and two already-live broken posts were deleted and reposted correctly.
5. **A workflow file with an unquoted `on:` key silently failed to register with GitHub Actions at all** (YAML 1.1 boolean ambiguity with `on`/`off`) — fixed by quoting it.
6. **Reasoning-model token-budget failures, found three separate times with three different specific fixes** — this pipeline uses three different OpenRouter models across its AI features, and each needed different handling:
   - Two models (captions, the shadow classifier) needed `reasoning: {"effort": "low"}` explicitly set, or they'd burn their entire token budget on internal reasoning and return null content.
   - A third model (spec extraction, Gemini 2.5 Flash Lite) needed the *opposite* — explicitly setting any reasoning effort broke it; omitting the parameter entirely was what made it reliable.
   - The caption feature hit this a second time today, in a different form: upgrading the prompt to require *analytical reasoning* ("explain why this is noteworthy") rather than plain creative writing pushed token consumption higher even at low effort, causing mid-sentence truncation at the old budget. Fixed by raising the budget; confirmed reliable across 9 repeated real-API test calls afterward.
7. **A double-post safety net that worked, but only by accident**: investigated whether the Woot cross-feed duplication (see #3) could have caused an actual duplicate *post*, not just a duplicate database row. Traced through the real posting loop and confirmed it couldn't, under normal conditions — but the reason was an in-memory dict update that happened to run before the second copy was checked, not a deliberate guard. The dedup fix (#3) converted this from "usually true, by luck" to "structurally can't happen."
8. **A related, still-open gap, not yet fixed**: `seen_deals` only gets updated inside a successful-post branch. If a webhook call actually succeeds server-side but the HTTP response is lost before the code sees it, the dedupe state never updates — and since it persists across runs via Supabase, that specific item could look "never posted" on a *future* run and genuinely post twice. Rare (requires a network failure at exactly the wrong moment), not fixed, worth scoping at some point.

---

## 6. AI features (OpenRouter) — three distinct, independently-tested features

All three share a fail-open design principle: any failure (missing key, network error, malformed response, failed validation) falls back to a safe default rather than blocking a post or breaking a run.

### 6.1 AI-written captions → upgraded today to data-backed "verdicts"

`build_ai_caption()` generates the text for both the Bluesky auto-post and the private Discord channel's copy-paste mirror. Originally written as engaging marketing-style copy; **upgraded today (Feature 2)** to a more restrained, analytical style: 1-2 sentences explaining *why* a deal is specifically noteworthy, grounded in real signals — the item's actual specs (from §6.3), and real Supabase price-history context (whether this is a genuine all-time low, or what the tracked floor price has been). No hype words. Anti-hallucination instruction forbids stating any spec not explicitly given.

**Hashtags are deliberately *not* restricted to a fixed allow-list.** This was an explicit decision point today — a stricter alternative (a hard 2-tag allow-list) was considered and rejected in favor of keeping the existing contextual, per-item hashtag variety already shipped and validated (real output includes tags like `#SSDDeals`, `#BaldursGate3`, `#GamingMonitor` — specific to what the deal actually is). A light sanity check (≤4 tags, well-formed) replaces the allow-list as the actual safety net.

Three-tier fallback: primary paid model → free fallback model → a plain mechanical template (`build_x_caption()`), which can never fail since it's pure string formatting. The LLM never generates the URL itself — it's appended in code — specifically so the model can't accidentally mangle it and break the Bluesky link facet.

### 6.2 Shadow-mode desirability classifier — built, not yet gating real posts

`classify_desirable_deals()` runs once per run (batched, not per-deal) against whatever actually posted, judging each as KEEP/DROP for "would a PC-building/gaming enthusiast genuinely want this" — beyond just clearing the keyword/discount filters. Reports to a dedicated Discord channel for review. **Deliberately not wired as an actual filter yet** — a wrong DROP would be invisible (a good deal silently never posted), so the plan is to review its judgment against real deals over time before ever letting it gate anything.

### 6.3 Clean title + spec extraction — built today (Feature 1)

`extract_clean_specs()` turns a messy retail title (e.g. *"Crucial P3 Plus 2TB PCIe Gen4 3D NAND NVMe M.2 SSD, up to 5000MB/s - CT2000P3PSSD8"*) into a clean product name plus up to 4 short, verified technical specs, feeding both the Discord embed and the caption features above. Scoped to Woot/Best Buy only — Steam titles are already clean and don't have hardware specs to extract. Validated to allow **zero** specs when a title genuinely has nothing worth calling out, rather than forcing a minimum — confirmed in testing that the model correctly returns an honest empty list for a generic item rather than inventing something to satisfy a schema.

### 6.4 Bluesky rich link cards + clickable hashtags

Separately, Bluesky posts carry a real link-preview card (downloaded product image, uploaded as a blob, attached as an `app.bsky.embed.external` card) and clickable hashtag facets — both implemented via raw AT Protocol REST calls, no new dependency (consistent with the rest of the project's minimal-dependency approach; the official `atproto` SDK was considered and explicitly declined for this reason).

---

## 7. Testing infrastructure — added today

Previously, this project had zero automated tests — everything was verified via live, real API calls during development (a deliberate, consistent practice throughout, not a gap). Today added a proper stdlib `unittest` suite (no new dependency — runnable via `python -m unittest discover -s tests` or `pytest tests/` if pytest happens to be installed):

- **`tests/test_spec_extraction.py` — 13 tests**: valid extraction, honest zero-spec responses, missing API key, network timeout, HTTP 500, malformed JSON, non-object JSON, oversized title/spec fields, too-many-specs, and (via the real pipeline code path, not a reimplemented condition) the Steam-skip logic.
- **`tests/test_deal_verdict.py` — 11 tests**: price-history context (`is_new_low`, known floor price) actually reaching the prompt, specs reaching the prompt, fallback on model failure/oversized output/spammy hashtags, contextual hashtags being preserved rather than restricted, and the 300-character Bluesky limit holding under the new caption style.
- **24 tests total, all passing**, confirmed both locally and on real GitHub Actions infrastructure (visible as a distinct CI step ahead of the actual bot run).

---

## 8. Open items

1. **Best Buy API key still pending.** Code is ready; the query-encoding logic in particular has never been exercised against a real key and is worth a specific check once it arrives.
2. **Shadow classifier not yet promoted to a real filter** — needs more real-world runs reviewed before trusting it to gate posts.
3. **Webhook false-negative dedupe gap** (§5, item 8) — real, rare, not yet fixed.
4. **One scheduled run was silently skipped by GitHub** with no root cause found (not an active GitHub incident, not a repo/billing issue) — a single occurrence so far, worth treating as a pattern if it recurs rather than a settled problem.
5. **Reasoning-effort behavior is genuinely model-specific, not a fixed rule** — three different OpenRouter models needed three different configurations this session (see §5, item 6). Worth re-verifying empirically, not assuming, whenever a new model gets added to this pipeline.

---

## Handoff summary

As of this session, `deal_bot.py` runs unattended on a 4-hour GitHub Actions schedule, pulling from Woot and Steam (Best Buy pending its API key), filtering through keyword/discount/price-history gates, and posting to 7 Discord channels and Bluesky. Two AI features are fully live and tested against real data: data-backed caption "verdicts" (upgraded today from generic marketing copy) and clean title/spec extraction (built today) — both fail open to safe defaults on any failure. A third AI feature, a desirability classifier, is deliberately running in observation-only shadow mode pending more real-world review. Bluesky posts carry rich link-preview cards and clickable hashtags via raw AT Protocol calls. A 24-test stdlib suite now runs in CI ahead of every scheduled execution, wired so a test failure is visible but can never silently block the unattended pipeline. Five open items remain (above), none blocking normal operation. For exact schemas, commands, and the full historical bug list, see `HANDOFF.md` in this repo.
