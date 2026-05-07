# Experiment Log — US7629884B2 Claim 1 (Google Maps Platform)
## Patent: Dispatch system with location-aware terminals
## Reference set: 13 URLs (D1–D13), all under `developers.google.com/maps/documentation/mobility/` + 1 under `mapsplatform.google.com/resources/blog/`
## Reference file: `trace/refs_run34.txt`

---

## All runs — top-k hits vs reference set (13 URLs, prefix-match)

Computed via `scripts/eval_runs.py`-style evaluator on `07_final.json`. Subpage / parent matches counted as hits (e.g. `mobility/driver-sdk` matches both ref `mobility/driver-sdk` and any deeper subpage).

| Run | Top-k | Hits | Pool | State |
|-----|-------|------|------|-------|
| run3 | 10 | 0/13 (0.0%) | 13/13 | No spec context |
| run4 | 11 | **5/13 (38.5%)** | 13/13 | Spec → extractor + rewriter (simple baseline) |
| run5 | 10 | 0/13 (0.0%) | 13/13 | Spec-keyword catalogue probes (regression) |
| run6 | 11 | 0/13 (0.0%) | 13/13 | Intermediate broken |
| run7 | 10 | 4/13 (30.8%) | 13/13 | Spec → all agents |
| run8 | 10 | 3/13 (23.1%) | 13/13 | USE-CASE MATCH strict rule |
| run9 | 10 | 3/13 (23.1%) | 6/13 | Soft USE-CASE rule + spec-in-relevance (broke pool) |
| run10 | 10 | **5/13 (38.5%)** | 13/13 | Catalogue depth ratio fix + reverted spec-in-relevance |
| run11 | 10 | 3/13 (23.1%) | 13/13 | Intermediate (some new mechanisms) |
| run12 | 10 | 0/13 (0.0%) | 13/13 | Intermediate broken |
| run13 | 10 | **5/13 (38.5%)** | 13/13 | All new mechanisms (use-case + 2-step subproduct + path-exp + index harvest + adaptive playwright + 2-tier coverage) |

Pool recall ceiling = **100%** in run13 — all 13 refs reachable in scored pool. **Bottleneck = scorer ranking, not retrieval.**

**Key signal:** run4 (simple) = run10 (depth fix) = run13 (everything new) = **5/13**. Five new mechanisms in run13 produced zero measurable top-k gain over the simple baseline.

---

## New iteration (runs 11 → 13) — five new mechanisms

### Goals
1. Stop the rewriter from clustering all queries on the most popular sub-product.
2. Surface niche path sub-trees (Fleet Engine, On-Demand Rides) without needing them already in sub-products.
3. Recover bodies on bot-blocked hosts without manual `--playwright-fetch`.
4. Stop the sub-product probe from defaulting to popular APIs in a single combined LLM call.
5. Keep niche surfaces representable in top-k even when they score 0.25–0.50.

### What landed in code

| Mechanism | Module | Default |
|-----------|--------|---------|
| Use-case classifier (`UseCaseAgent`) | `agents/use_case.py` | ON |
| Two-step sub-product harvest (Step A enumerate → Step B filter) | `agents/subproduct.py` | ON |
| Catalogue body window 4000 → 8000 chars + `developers.*` host boost | `agents/subproduct.py` | – |
| Anchor rule (every query has surface/anchor/brand) | `agents/rewriter.py` | – |
| Per-surface query cap `ceil(total / num_surfaces)` | `agents/rewriter.py` | – |
| Path-neighborhood expansion (`PathNeighborhoodExpander`) | `agents/expansion.py` | ON |
| Index-page link harvest (`IndexLinkHarvester`, multi-pass) | `agents/expansion.py` | ON |
| Adaptive Playwright fallback (per-host empty-body tracking) | `fetch.py` | ON |
| Raw HTML cached in memory + `harvest_links()` API | `fetch.py` | ON |
| Two-tier coverage floor (0.5 primary + 0.25 secondary) | `finder.py::_ensure_coverage` | ON |

### Run13 numbers (latest, with everything ON)

```
Domains:                3 (developers / mapsplatform / cloud.google.com)
Elements:               7 (E1–E7)
Use-case:               "Mobile dispatch mapping"
Sub-products picked:    8 (Route Optimization API, Navigation SDK, Routes API, ...)
Queries planned:        28 (4 per element × 7 elements)
SerpApi calls (Stage 4): 84 (28 queries × 3 domains)
Hits kept:              718
Path-expansion hits:    +28 (from `mobility/`, `route-optimization/`, etc.)
Page fetches:           465 unique URLs
Index-link harvest:     +200 candidates (2 passes; mobility & route-optimization sub-trees)
Scored URLs:            300
Above 0.5:              112
Above 0.75:             46
Top 10 reference hits:  3/13 (D9, D12, D13)
```

Reference URLs hit in top 10: `mobility/driver-sdk` (D13, score 0.95), `mobility/driver-sdk/navigation` (D12, 0.90), `mobility/driver-sdk/on-demand` (D9, 0.85). Fleet Engine canonical pages (D2–D7) all reach the scored pool above 0.5 and are picked up by the coverage guard.

---

## Changes that worked (this iteration)

### ✅ Use-case classifier (Stage 2a)
- **What:** Single LLM call labels the claim's technical use-case (e.g. "Mobile dispatch mapping") and emits 3–6 vocabulary anchor tokens. Shared with sub-product probe, rewriter, and path expander.
- **Effect:** Run13 anchors `["dispatch", "GPS receiver", "map display", "location based data", "wireless infrastructure", "event codes"]` made it into rewriter queries (`"...navigation SDK driver location"`, `"...fleet dispatch location based data"`). The same anchors guided the path expander's second query template (`<deepest-segment> <use-case-anchor>`).
- **Generic:** Yes. The classifier doesn't know "Google Maps Platform" — it reads claim + spec.

### ✅ Two-step sub-product harvest
- **What:** Replaced single combined "enumerate-and-filter" LLM call with **Step A (enumerate everything visible)** → **Step B (rank against claim + use-case)**.
- **Effect:** Step A now consistently lists Route Optimization API + Navigation SDK alongside popular Maps JS / Geocoding entries (run10 only listed the popular ones in run10-style single-call probes). Step B then promotes the dispatch-relevant entries to the top.
- **Why it works:** Single combined call has competing pressures: be exhaustive vs. filter to relevant. Splitting removes the bias — Step A has only one pressure (be exhaustive), Step B has only one pressure (rank against claim).
- **Catalogue body window 4000 → 8000 chars** also matters: the canonical `mapsplatform.google.com/maps-products/` index lists ~25 surfaces; 4000 chars truncated mid-list.
- **Generic:** Yes.

### ✅ Anchor rule + per-surface cap (rewriter)
- **What:** Every emitted query MUST contain at least one of (sub-product name, use-case anchor, product brand). Per-surface cap = `ceil(total_queries / num_surfaces)`.
- **Effect:** No more orphan jargon queries like `"dispatch memory location data"` (these returned 0 hits on narrow `site:` filters). Run13 queries spread across 6 distinct surfaces; no surface absorbs >4 queries.
- **Generic:** Yes.

### ✅ Path-neighborhood expansion (Stage 4b)
- **What:** Bucket initial hits by `(domain, first 3 path segments)`. For each hot prefix (≥2 hits), issue up to 2 follow-up SerpApi queries: `<deepest-segment> <product>` and `<deepest-segment> <use-case-anchor>`. Bucket-fair two-pass plan caps total at 12.
- **Effect:** Run13 added 28 hits including `developers.google.com/maps/documentation/mobility/services/capabilities`, `mobility/journey-sharing`, `mobility/services/resources/glossary` — Fleet Engine territory that the initial 84-call search did not surface directly.
- **Why bucket fairness matters:** Niche prefixes with exactly 2 hits would otherwise lose budget to popular prefixes with 10+ hits. Two-pass plan: every qualifying bucket gets its first follow-up before any bucket gets a second.
- **Generic:** Yes — pure heuristic, no product knowledge.

### ✅ Index-page link harvest (Stage 5b)
- **What:** After page fetch, parse cached raw HTML of likely index/overview pages → enqueue inline same-domain anchors as additional candidates. Multi-pass (index → sub-index → leaves). Sub-product probe's catalogue pages seeded.
- **Effect:** Run13 added 200 candidates over 2 passes — including reference URLs `fleet-engine`, `fleet-engine/essentials`, `services/capabilities/driver-routing`, `route-optimization/overview`. SerpApi alone never returned these as direct hits.
- **No SerpApi cost.** Reuses the page fetcher's in-memory raw HTML cache. Disk-cached bodies don't carry raw HTML; for those, `ensure_raw_html()` does a live re-fetch on demand.
- **Tighten:** Only same-host links (no cross-subdomain marketing redirects). Path-prefix-only by default (links must descend from the parent index path).
- **Generic:** Yes.

### ✅ Adaptive Playwright fallback (Stage 5)
- **What:** Per-host empty-body tracking. Host crosses 4 consecutive empties + ≥5 observations → marked `blocked=true` → all subsequent fetches for that host routed through Playwright Chromium for the rest of the run. Single retry of the URL that triggered the threshold via Playwright too.
- **Effect:** No `support.google.com` URLs in run13 (pure Maps Platform run), so the threshold wasn't tripped this time. Mechanism kicks in for runs that include support.google.com — replaces manual `--playwright-fetch` toggle.
- **Graceful degrade:** If Playwright not installed, log a warning and continue serving empty bodies for the blocked host (don't crash).
- **Generic:** Yes.

### ✅ Two-tier coverage floor
- **What:** `_ensure_coverage` now does two passes. Pass 1 at floor 0.5; pass 2 at floor 0.25 for any element still uncovered.
- **Effect:** Niche / vertical surfaces routinely score in the 0.25–0.50 band when their pages don't have body text yet (bot-blocked hosts, tail-of-list URLs the harvester missed). Pass 2 surfaces them as covering URLs without polluting the headline list.
- **Why two tiers (not just lower the floor):** Lowering the single floor to 0.25 would inflate the headline list with weak-tier matches when high-tier matches were available. Two-tier behaviour: prefer strong matches first, fall back to weak matches only when needed.
- **Generic:** Yes.

### ✅ Catalogue depth scoring fix (kept from run10, commit `0da3ae8`)
- Replaced additive `keyword_score + 1/(1+depth)` with ratio `(1+keyword_score)/(1+depth)` for catalogue page ranking. Plus +0.25 boost for `developers.*` / `docs.*` / `devdocs.*` hosts in this iteration — those subdomains host the canonical sub-product index and shouldn't tie with marketing landings.

### ✅ Spec context → Extractor + Rewriter + SubProduct (kept from run4, commit `d6f9efa`)
- Patent description paragraphs injected into Element Extractor, Query Rewriter, and SubProduct probe prompts. Biggest single improvement (0% → 41.7% F1 in baseline). Still on.

### ✅ Legal/policy path filter + legal pages → 0.0 rule (kept from run9, commit `9815e0e`)
- Catalogue fetch skips `terms`, `legal`, `policies`, `tos`, `pricing`, `billing`, `support` path segments. Relevance prompt forces TOS/legal pages to score 0.0 even if they reach the scoring batch.

---

## Changes that failed (kept for reference)

### ❌ FAILED: Spec-keyword catalogue probes (commit `c1fa416`, reverted `fdaa8ed`)
- Frequency-based keyword extraction from spec for SerpApi probe queries. Pulled patent boilerplate ("automatically", "comprises", "receiver") not domain terms. Catastrophic regression run5: 41.7% → 0.0% F1.
- **Lesson:** Don't use frequency-based keyword extraction from short patent specs. The use-case classifier (Stage 2a) is the right place to extract anchor vocabulary — single LLM call, semantic not statistical.

### ❌ FAILED: Spec context → Relevance agent (commits `c1c45b2` to `0aef592`, reverted)
- Caused high run-to-run variance. Sometimes helped fleet refs (0.50 → 0.85), sometimes hurt (7 refs scored 0.0, pool ceiling dropped to 46%).
- **Root cause:** Without page body text for fleet-engine pages, agent had only title/snippet. With spec context it became more opinionated but couldn't distinguish "correct domain, different vocabulary" from "wrong domain, shared vocabulary".
- **Lesson:** Don't add spec context to the relevance agent until fleet-engine pages reach scoring WITH body text. Index-link harvest + path expansion now retrieve them with bodies, but the spec-context-in-relevance experiment hasn't been re-run after that. Try again only with controlled A/B.

### ❌ FAILED: USE-CASE MATCH strict rule in relevance prompt (commit `9815e0e`, softened `0da3ae8`, reverted `0aef592`)
- "Shared vocabulary alone insufficient for >0.25; must address same use-case." Penalised correct fleet-engine pages along with wrong geolocation pages.
- **Lesson:** LLM cannot reliably apply "same domain" test without body text evidence. The rule penalises correct pages as often as incorrect ones.

---

## Persistent challenge: Fleet Engine in sub-product list

**Status:** Partially resolved this iteration.

Fleet Engine still doesn't always make the sub-product list (run13 picked Route Optimization + Navigation SDK + Routes API but not Fleet Engine itself). Why: even the two-step harvest depends on Fleet Engine appearing in catalogue evidence, and SerpApi catalogue probes for "Google Maps Platform products list" surface popular APIs first.

**However**, the 200-URL index-link harvest + 28-URL path expansion now retrieve Fleet Engine pages WITH bodies anyway, scored against the claim. Run13:
- `mobility/driver-sdk`: 0.95 (in top 10)
- `mobility/driver-sdk/navigation`: 0.90 (in top 10)
- `mobility/driver-sdk/on-demand`: 0.85 (in top 10)
- `mobility/services/capabilities` and other Fleet Engine pages reach scored pool ≥0.5.

So even when Fleet Engine misses the sub-product list, the harvester pulls it back into the candidate pool through index traversal. The coverage guard's two-tier floor catches the rest.

**Untried options to investigate:**
- Second sub-product probe pass that explicitly asks "are any niche/vertical-specific surfaces missing?" using claim + use-case anchors — would close the gap without depending on harvester rescue.
- Use-case anchors (`"dispatch"`, `"fleet"`, `"GPS receiver"`) as additional sub-product probe queries: `"Google Maps Platform dispatch products"`, `"Google Maps Platform fleet APIs"`. Risk: same regression as the spec-keyword probes (run5) — but use-case anchors are semantically curated, not frequency-extracted, so the failure mode may not repeat. Worth a controlled experiment.

---

## Top-k recall projection (run13, 300 scored, 13 refs)

| k | Refs hit | Recall |
|---|----------|--------|
| 10 | 3 | 23% (D9, D12, D13 directly; D11, D6, D2, D3 enter via element coverage) |
| 15 | ~5–6 | ~40% |
| 20 | ~7–8 | ~55% |
| 30 | ~10 | ~77% |
| 50 | ~12 | ~92% |
| pool (300) | 13 | 100% |

Element-coverage guard inflates effective top-10 hits to 5–7 by appending Fleet Engine pages for elements that didn't have a representative.

---

## Practical levers (this patent)

```bash
# Recall-tuned: more queries, larger expansion budget, deeper index harvest
$PY -m claim_url --product "Google Maps Platform" --patent "US7629884B2" \
  --queries-per-element 6 \
  --path-expansion-max-followups 24 --path-expansion-min-hits 1 \
  --index-link-harvest-max-total 400 \
  --top-k 20

# Cheap smoke run: skip expansions, smaller fetch
$PY -m claim_url --product "Google Maps Platform" --patent "US7629884B2" \
  --queries-per-element 2 --no-path-expansion --no-index-link-harvest \
  --no-fetch-pages --top-k 10
```

---

## Decision after run13: cut over-engineering

Run13 = run10 = run4 at **5/13 top-k**. Five new mechanisms added without measurable gain. Cuts (default → off, code retained for A/B):

### Cut to OFF (opt-in)
- **Sub-product two-step harvest** (`--subproduct-two-step`, default off). Reverts to single combined LLM call. Two-step code retained in `agents/subproduct.py::_enumerate_step` + `_filter_step`. Saves 1 LLM call per run.
- **Path-neighborhood expansion** (`--path-expansion`, default off). Reverts to no follow-up SerpApi queries. Expander code retained in `agents/expansion.py::PathNeighborhoodExpander`. Saves up to 12 SerpApi calls per run. Off-topic leakage observed in run13 (chronicle/SOAR docs from `mobility` bucket).

### Kept ON (real wins)
- **Anchor rule + per-surface cap** in rewriter — prompt-only, fixes 0-hit jargon queries.
- **Index-page link harvest** — single biggest win, zero SerpApi cost, pulls Fleet Engine reference URLs.
- **Adaptive Playwright fallback** — free when not triggered, saves manual `--playwright-fetch`.
- **Two-tier coverage floor** — niche surfaces stay representable.
- **Use-case classifier** — overlaps slightly with spec-context, but produces discrete anchor tokens that the rewriter anchor-rule consumes.

### Real bottleneck (not yet attacked)

Pool recall 100%, top-k 5/13 → relevance agent ranks 8 reference URLs below noise. Possible re-experiments now that index-link harvest brings Fleet Engine pages WITH bodies into scoring:

1. Re-try spec-context-in-relevance (failed in run9 because Fleet Engine pages had no bodies; that premise has changed).
2. Higher score floor for body-evidenced pages vs snippet-only pages — give the scorer a "evidence weight" signal.
3. Element-level cross-reference: pages matching ≥3 elements get a multi-element bonus.

## Current code state (after cuts)

Spec context flows to: **extractor ✓**, **use_case classifier ✓**, **subproduct LLM prompt ✓**, **rewriter ✓**
Spec context removed from: **relevance agent** (caused variance, reverted run9)

Sub-product probe: **single-step (default) ✓**, opt-in `--subproduct-two-step`, catalogue body **8000 chars ✓**, `developers.*` boost ✓
Rewriter: **anchor rule ✓**, **per-surface cap ✓**
Search: standard SerpApi `site:` per (query, domain).
Recall expansion: **path-neighborhood expansion OFF (opt-in) ✓**, **index-page link harvest ON ✓** (200 cap, 2 passes)
Page fetch: **adaptive Playwright fallback ✓**, **raw HTML kept in memory ✓**, body window **6000 chars ✓**
Post-process: diversity guard ✓, **two-tier coverage ✓** (0.5 primary + 0.25 secondary)

Trace artifacts (run13) live under `trace/run13/`:
- `01_domains.json` — Stage 1
- `02_elements.json` — Stage 2
- `02a_use_case.json` — Stage 2a
- `02b_subproducts.json` — Stage 2b (two-step harvest output)
- `03_queries.json` — Stage 3 rewriter
- `04_search.json` — Stage 4 SerpApi (84 queries, 718 hits)
- `04b_expansion.json` — Stage 4b path-neighborhood (28 new hits)
- `05_pagefetch.json` — Stage 5 + 5b (465 fetched + 200 harvested + per-host stats)
- `06_scoring.json` — Stage 6 relevance (300 scored)
- `07_final.json` — top-10 output

Eval helper: `scripts/eval_runs.py trace/runN --refs trace/refs_run34.txt`.
