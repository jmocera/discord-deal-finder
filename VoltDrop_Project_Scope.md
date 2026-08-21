# VoltDrop — Project Scope

**Scope note:** §§1-7 and §11 (technical pipeline, bugs, AI features, testing, open items) are drawn directly from verified work in this session — live API tests, real GitHub Actions runs, actual git history. §§8-10 (affiliate programs, the manual Amazon workflow, brand/growth) cover business-development work outside the automated pipeline, reported by the operator rather than independently verified via this session's tool use — flagged inline where they appear, **except §9.1**, which documents `vet_amazon_deal.py`, a tool built and verified in this session (real OpenRouter API calls, real CLI runs) that assists the operator-reported manual workflow without being part of it. For deep technical reference on the pipeline itself (exact schemas, commands, full bug list), see `HANDOFF.md` in this repo — this document is the higher-level narrative; that one is the operational reference.

**Repo:** https://github.com/jmocera/discord-deal-finder (private)

---

## 1. System overview

VoltDrop's automated side is a Python package (`deal_bot/`, run via `python -m deal_bot`) that runs unattended on a GitHub Actions schedule (every 4 hours), with no server and no persistent local process:

```
 Woot API    Best Buy API    Steam API
    │             │              │
    └─────────────┼──────────────┘
                   ▼
            deal_bot (GitHub Actions, cron every 4h)
                   │
   ┌───────────────┼─────────────────────────┬──────────────┬──────────┐
   ▼               ▼                         ▼              ▼          ▼
Supabase      Discord webhooks (7 + 3   Bluesky (AT    OpenRouter    Watchdog
(seen_deals,  shadow) Woot / Best Buy / Protocol, raw   (captions,   (hourly
price_history, Steam / Digest /        REST, no SDK)   spec+analysis, dead-man's
run_log,       Run Log / Shadow                        classifier,  switch)
posted_deals)  Classifier / Scorer /                   scorer,
               Categorizer                              categorizer,
                                                        weekly digest)
```

Best Buy is currently dormant in the live pipeline — its API key is still pending approval as of this writing, so that source contributes zero deals until it's granted.

Separately, three companion GitHub Actions workflows run off the main cadence: `weekly_digest.yml` (Mondays at noon — AI-written roundup of the week's posted deals), `watchdog.yml` (hourly — a dead-man's switch that alerts if the bot stops logging runs), and `keepalive.yml` (monthly — prevents GitHub auto-disabling the schedule triggers).

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

Chosen specifically because GitHub Actions runners are ephemeral — a local JSON file for dedupe state would never survive between scheduled runs. Four tables:

- **`seen_deals`** — dedupe state, keyed by deal ID (`source:id`), tracks last-seen price so a further price drop can re-trigger a post.
- **`price_history`** — one row per deal *per day* (not per run — see §5 for why that distinction mattered), upserted on `(deal_id, observed_date)`.
- **`run_log`** — one row per run, success or failure, with full counts — written even when the run crashes partway through, and mirrored live to a dedicated Discord channel (`RUN_LOG_WEBHOOK_URL`) so a failure is never silent.
- **`posted_deals`** — append-only log of every deal that actually posted (with title/url/prices), written on each successful post; backs the weekly digest and is pruned on a 90-day TTL.

All Supabase access goes through a single shared HTTP transport (`deal_bot/transport.py`) with bounded retry/backoff, so a transient blip can't silently produce an empty seen-map and double-post.

---

## 4. Deployment

- **GitHub Actions** (`.github/workflows/deal_bot.yml`): `cron: "0 */4 * * *"`, `workflow_dispatch` for manual runs, Python 3.13, 15-minute timeout. The posting pipeline is phased (deterministic filter → batched AI enrichment → post loop) — the split that will let the shadow AI judges gate posts later.
- **`weekly_digest.yml`**: Mondays at noon UTC; runs `deal_bot.weekly_digest` (AI roundup of `posted_deals` to the digest channel + Bluesky). Has a `no_bluesky` dispatch input for safe E2E testing.
- **`watchdog.yml`**: hourly dead-man's switch (`deal_bot.watchdog`) — alerts if no `run_log` row lands within 6h.
- **`keepalive.yml`**: runs monthly, pushes an empty commit — GitHub auto-disables a workflow's *schedule* trigger after 60 days of no git commit activity (workflow runs don't count toward that), so this exists purely to keep the schedule itself from silently lapsing.
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
9. **PostgREST rejects an unbounded DELETE (2026-08-21)** — the weekly digest's `--clear` initially sent a DELETE with no WHERE clause and got HTTP 400. Fixed by scoping to seed rows (`id` `like 'seed:%'`).
10. **`load_seen` silently failing was a double-post risk (2026-08-21)** — a transient Supabase failure returned `{}`, so every deal looked new. Now returns `None` on a hard failure and `run_once` bails and logs before fetching feeds.
11. **A broken test setUp/tearDown cascaded config mutations (2026-08-21)** — a filtering test saved only some threshold values, so its tearDown raised and left mutated config leaking into later test modules. Fixed by saving all four thresholds and pinning a deterministic baseline.

(§5 is the deep bug list; HANDOFF.md's "Bugs found and fixed this session" is the same list with more line-level detail.)

---

## 6. AI features (OpenRouter)

All AI features share a fail-open design principle: any failure (missing key, network error, malformed response, failed validation) falls back to a safe default rather than blocking a post or breaking a run. The three original features are detailed below; newer ones are summarized. For current model choices see HANDOFF.md's OpenRouter section.

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

### 6.5 Features added later (2026-08-21)

- **Deal quality scorer** (`ai/deal_scorer.py`) — SHADOW MODE. One batched call per run rating each posted deal 1-10 for a PC-building/gaming audience; would drop deals below `MIN_QUALITY_SCORE`, but gates nothing yet.
- **Deal analysis** (ai/deal_analyst.py) — LIVE. A 2-3 sentence "why this is noteworthy" block added to the Discord embed's "Analysis" field; batched with a per-item fallback.
- **Category tagger** (ai/categorizer.py) — SHADOW MODE. Tags each posted deal into storage/display/component/peripheral/game/other; not yet used to gate or route.
- **Weekly digest** (weekly_digest.py) — a Monday AI-written roundup from the `posted_deals` table.

Spec extraction + analysis run **batched** (one OpenRouter call per phase, per-item fallback), which is the dominant per-run latency/cost control as deal volume grows.

### 6.6 Reliability underpinnings (2026-08-21)

A shared `deal_bot/transport.py` gives every outbound call bounded retry/backoff (plus the `load_seen`→`None` bail and the hourly `watchdog` heartbeat). Details in HANDOFF.md's Reliability section.

---

## 7. Testing infrastructure

Previously, this project had zero automated tests — everything was verified via live, real API calls during development (a deliberate, consistent practice throughout, not a gap). A stdlib `unittest` suite was added (no new dependency — runnable via `python -m unittest discover -s tests` or `pytest tests/` if pytest happens to be installed), and has grown alongside each AI feature as it shipped: Feature 1 (clean title/spec extraction, §6.3), Feature 2 (data-backed deal verdicts, §6.1), and now Feature 3 (the Amazon vetting assistant, §9.1) each landed with its own dedicated test file rather than the suite trailing the code:

- **`tests/test_spec_extraction.py`** — spec extraction (per-deal + the batched path), and via the real `_process_deals` code path the Steam-skip logic.
- **`tests/test_deal_verdict.py`** — caption "verdict" prompt context, fallbacks, and Bluesky length limits.
- **`tests/test_amazon_vetting.py`** — the standalone Amazon vetting tool (Feature 3, §9.1): ASIN extraction, canonicalization, strict field validation, deterministic risk assessment, `#ad`-first copy, and the shared client fail-open paths.
- **`tests/test_deal_scorer.py`** / **`tests/test_categorizer.py`** — the shadow AI features' parse + fail-open behavior.
- **`tests/test_deal_analyst.py`** / **`tests/test_weekly_digest.py`** — the analyst and the weekly digest (fetch/prune/seed/clear, retry behavior, dry-run, exit codes).
- **`tests/test_pipeline.py`** — the phased pipeline: `_skip_reason` boundaries, `run_once`'s `load_seen`-None bail, and the batch spec/analysis fallbacks.
- **`tests/test_watchdog.py`** — the dead-man's switch: freshness/staleness and the `run_log` ordering query.

**171 tests total, all passing** (`python -m unittest discover -s tests -p "test_*.py"`), wired into CI ahead of the bot run with `if: always()` so a regression is visible but can never silently block the scheduled execution.

---

## 8. Affiliate programs

*(Reported by the operator; not independently verified via this session's tool use — application/approval statuses can change without that being reflected here.)*

| Program | Status | Notes |
|---|---|---|
| Amazon Associates | Approved and active. Tag: `voltdrop05-20` | Not wired into the automated pipeline — Amazon isn't a `deal_bot` data source. Applied manually via `?tag=` on manually-sourced links. |
| CJ Affiliate (covers Woot) | Application submitted, under review | Response time reportedly inconsistent per Woot forums — could be weeks. |
| Impact.com (covers Best Buy + Walmart) | Researched, not yet submitted | Recommended next step. |
| Best Buy Developer API | Key requested, pending approval | Separate from the affiliate program above — this is data access, not commission (see §2). |
| Newegg | No public affiliate path found | Real path would be an affiliate network (possibly CJ), not yet pursued. |
| Steam | No affiliate program exists | Long-standing Valve policy — kept as a content-variety source only, not a monetization path. |

**FTC disclosure:** `#ad` is applied on manual Amazon posts using the affiliate tag. **Not yet relevant to the automated pipeline** — none of Woot/Best Buy/Steam currently carry affiliate codes, since none of those programs are approved yet. This will need to be built into `deal_bot` once CJ/Impact.com approvals land (see §11).

## 9. Manual deal-posting workflow (Amazon)

*(Reported by the operator; entirely separate from `deal_bot` — Amazon isn't a scripted data source, so none of this runs through the automated pipeline.)*

The operator sources deals manually via screenshots, drafting captions using only visible, real data. Credibility checklist applied to every manual find:

- Sold & shipped by Amazon.com (not third-party) = green flag; a third-party seller combined with a thin review count (~20) = red flag.
- Amazon's own "Typical price" (an algorithmic reference based on real sale history) is treated as more trustworthy than a seller-set "List Price," which can be inflated.
- `#ad` required at the *front* of the post per FTC rules — `#partner`/`#collab` are explicitly not treated as sufficient substitutes.
- Bluesky doesn't reliably generate link-preview cards for `amzn.to` links — worked around by manually attaching the product photo.

### 9.1 `vet_amazon_deal.py` — vetting assistant (Feature 3, session-built and verified)

*(Unlike the rest of §9, this subsection is drawn from this session's own work — real OpenRouter API calls and real CLI runs, not operator reporting.)*

A standalone CLI that formalizes the checklist above into a repeatable tool, rather than relying on the operator applying it by hand each time. **Explicitly not wired into `deal_bot` or the GitHub Actions cron** — same as the rest of §9, Amazon stays a manual, operator-run workflow; this tool assists that workflow, it doesn't automate it away.

- **Three input modes:** a product URL (best-effort page fetch — a plain GET with a normal browser User-Agent, no proxies, no CAPTCHA-solving, no fingerprint spoofing; when Amazon blocks it, which is common, the tool says so and still returns the canonicalized affiliate link from the ASIN alone, just without full field extraction), raw pasted page text, or a product screenshot (vision).
- **Extraction via OpenRouter, not decision-making:** text/URL mode reuses `google/gemini-2.5-flash-lite` — the same model, and the same "omit the `reasoning` parameter entirely" finding, as Feature 1's spec extraction (§6.3, §5 item 6). Vision/screenshot mode uses `google/gemini-2.5-flash`. Every extracted field (`clean_title`, `sale_price`, `list_or_typical_price`, `seller_type`, `review_count`, `rating`) is strictly type/range-validated with no coercion — a malformed field falls back to `null` rather than being guessed, same posture as `extract_clean_specs`.
- **Risk assessment is deterministic Python, not an LLM judgment call:** `compute_risk_assessment()` codifies the exact manual checklist above in code — seller type other than "Sold/Shipped by Amazon", review count under 100, rating under 4.0, and a sale price that isn't actually below the reference price all produce an explicit warning; the model's only job is extracting the underlying facts.
- **URL canonicalization:** the affiliate link is rebuilt from the ASIN alone as `https://www.amazon.com/dp/{ASIN}?tag=voltdrop05-20` — that reconstruction is what guarantees `ref=`/`qid=`/`sr=`/every other tracking param is dropped, rather than stripping params one at a time. ASIN extraction is regex-first (zero network calls for normal `/dp/`/`/gp/product/` links) and only falls back to resolving a redirect for shortened links (`amzn.to`, `a.co`).
- **FTC disclosure enforced structurally:** `#ad` is the literal first four characters of both the generated Discord and Bluesky copy — never appended, never conditional — including under Bluesky's 300-character truncation path.
- **66 new stdlib `unittest` tests** (`tests/test_amazon_vetting.py`, see §7).

## 10. Brand & growth

*(Reported by the operator; non-technical, included for completeness.)*

- Hashtag strategy differs by platform: on X, hashtags are treated as low-value under the current algorithm (0-1 max, `#ad` only when required); on Bluesky, hashtags genuinely power community-run custom feeds, but only after manually confirming a tag maps to a real, active feed via Bluesky's own Feeds-tab search rather than third-party trending tools.
- X Premium adopted, with the expectation that it amplifies existing activity rather than creating growth on its own.
- A link-placement A/B test (link-in-post vs. link-in-reply) is in progress; early data only, not yet conclusive.
- A paid Discord membership tier (gating deal *quality*, not *source*) is planned conceptually, explicitly deferred until there's a real audience.

---

## 11. Open items

1. **Best Buy API key still pending.** Code is ready; the query-encoding logic in particular has never been exercised against a real key and is worth a specific check once it arrives.
2. **Shadow classifier not yet promoted to a real filter** — needs more real-world runs reviewed before trusting it to gate posts. The pipeline restructure that promotion needs (phased `_process_deals`) is **done**, so this is now purely gated on shadow-data review.
3. **Webhook false-negative dedupe gap** (§5, item 8) — real, rare, not yet fixed.
4. **One scheduled run was silently skipped by GitHub** with no root cause found (not an active GitHub incident, not a repo/billing issue) — a single occurrence so far. This failure mode is now surfaced by the hourly **watchdog** rather than left to "absence of activity."
5. **Reasoning-effort behavior is genuinely model-specific, not a fixed rule** — three different OpenRouter models needed three different configurations (see §5, item 6), and the Gemma models needed a fourth (reasoning omitted). Worth re-verifying empirically, not assuming, whenever a new model gets added to this pipeline.
6. **No affiliate tagging or `#ad` disclosure exists yet on the automated pipeline's outputs** (Woot/Best Buy/Steam) — only manual Amazon posts currently carry disclosure (§9). This still needs to be built into `deal_bot` before any of the pending affiliate programs (§8) can actually monetize those sources.
7. **CJ Affiliate and Impact.com applications are both still pending/not started** — until either lands, the automated pipeline's Woot/Best Buy links have no monetization path even once #6 above is built.
8. **`vet_amazon_deal.py` (§9.1) isn't yet a required step** — nothing stops the operator from posting an Amazon deal by hand without running it first. Adoption is a process/discipline question, not a code one.
9. **`price_history` growth / `load_seen` pagination** — see HANDOFF's open items; fine at current volume, revisit before Best Buy roughly doubles deal count.

---

## Handoff summary

As of this session, `deal_bot` runs unattended on a 4-hour GitHub Actions schedule, pulling from Woot and Steam (Best Buy pending its API key), filtering through the phased pipeline (deterministic `_skip_reason` filter → batched AI spec/analysis → post loop), and posting to 7 Discord channels plus Bluesky. Live AI: data-backed caption "verdicts", clean title/spec extraction, and deal analysis — all routed through a shared retry/backoff transport. Shadow-mode AI (desirability classifier, quality scorer 1-10, category tagger) observes but gates nothing until enough real-world data is reviewed. An hourly **watchdog** heartbeat alerts if the bot ever stops logging runs. Bluesky posts carry rich link-preview cards and clickable hashtags via raw AT Protocol calls. A 171-test stdlib suite is wired into the same CI step ahead of every scheduled execution (`if: always()` on the bot-execution step), so a test failure is visible but can never silently block the unattended pipeline.

Separately, a new standalone tool, `vet_amazon_deal.py` (Feature 3, §9.1), formalizes the operator's existing manual Amazon credibility checklist into a repeatable CLI — text/URL/screenshot ingestion via OpenRouter, deterministic (non-LLM) risk assessment, ASIN-based affiliate-link canonicalization (`voltdrop05-20`), and `#ad`-first ready-to-copy output. It is explicitly **not** part of the automated pipeline and doesn't change automated-pipeline monetization status at all.

On the business side more broadly (§§8-10, operator-reported): Amazon Associates is approved and active but only feeds the manual posting workflow (now assisted by §9.1), not the automated pipeline; CJ Affiliate (Woot) and Impact.com (Best Buy/Walmart) applications are pending/not yet submitted; and no affiliate tagging or FTC disclosure exists yet on any *automated*-pipeline output — that gap (§11 item 6) is unchanged by Feature 3 and still needs to be built into `deal_bot` before those programs can monetize Woot/Best Buy traffic once approved.

Nine open items remain (§11), none blocking normal operation. For exact schemas, commands, and the full historical bug list, see `HANDOFF.md` in this repo.
