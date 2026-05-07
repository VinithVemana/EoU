# How the URL Finder Works — End to End

**Example used throughout:** `--product "Google Maps Platform" --patent "US7629884B2" --claim-number 1`

All numbers, queries, sub-products, scores, and example URLs in this doc are taken verbatim from the artifacts in `trace/run13/` (the most recent full run). When this doc says "84 SerpApi calls" or "8 sub-products picked" that is the literal value the pipeline emitted, not a hypothetical.

---

## What You Give It

```
Product:      Google Maps Platform
Patent:       US7629884B2
Claim number: 1
```

The tool fetches the claim text and patent description from the patent database (PCS API), then finds official documentation URLs that are evidence for each sentence in the claim.

---

## What the Claim Says (Plain English)

US7629884B2 Claim 1 describes a **dispatch system**:

1. A central "dispatch terminal" receives an address + event (e.g. a job order).
2. It looks up that address in a location database.
3. A mobile device ("second terminal") knows its own GPS location and sends it to the dispatch terminal.
4. The dispatch terminal stores the mobile's location.
5. When conditions are met (e.g. mobile is nearby), it sends the job address back to the mobile.
6. The mobile automatically shows that address on its map display.

In other words: **a driver app that gets dispatched to an address**.

---

## Pipeline at a Glance

```
INPUT
  ▼
[1] Domain discovery               (LLM + SerpApi probes)
[2] Claim element extraction       (LLM, with spec)
[2a] Use-case classification       (LLM)              ← KEPT
[2b] Sub-product probe             (SerpApi + fetch + 1 LLM call)
[3] Query rewriting                (LLM)
[4] SerpApi search                 (per query × domain)
[5] Page fetch (pass 1)            (HTTP / Playwright)
[5b] Index-page link harvest       (parse cached HTML) ← KEPT
[5c] Page fetch (pass 2)           (HTTP / Playwright) ← for harvested
[6] Relevance scoring              (LLM, batched)
[POST] Diversity guard + element coverage (two-tier floor)
  ▼
TOP-K
```

### What's NEW vs prior simple baseline (run4)
- **Use-case classifier (Stage 2a)** — kept. Single LLM call extracting anchor tokens shared with Stages 4–5.
- **Index-page link harvest (Stage 5b)** — kept. Zero-cost retrieval boost; pulls in Fleet Engine pages SerpApi never returned.
- **Adaptive Playwright fallback (Stage 5)** — kept. Auto-promotes bot-blocked hosts.
- **Anchor rule + per-surface cap (Stage 3)** — kept. Prompt-only constraints.
- **Two-tier coverage** — kept. Strict primary floor + relaxed fallback.

### What's available but OFF by default (opt-in A/B)
- **`--subproduct-two-step`** — splits Stage 2b into enumerate + filter (2 LLM calls). No measurable top-k gain on test patent (run10 single-step = run13 two-step = 5/13). Code retained for revisit.
- **`--path-expansion`** + family — Stage 4b. Follow-up SerpApi queries under hot path prefixes. Overlapped with index harvest, leaked off-topic. Code in `agents/expansion.py` for revisit.

Sections below describe the active default pipeline. Disabled stages mentioned where relevant.

---

## Stage 1 — Find the Official Domains

**Goal:** What websites does Google Maps Platform officially use?

The tool runs 5 SerpApi probes:
- `"Google Maps Platform official website"`
- `"Google Maps Platform official support"`
- `"Google Maps Platform documentation official"`
- `"Google Maps Platform site:google.com"`
- `"Google Maps Platform developer site"`

URLs collected, then one LLM call asks: *"Which of these domains are officially owned by Google Maps Platform?"*

**Run13 result (`01_domains.json`):**

| Domain | Confidence | Why |
|--------|-----------|-----|
| `developers.google.com` | 99% | Official developer docs and API references |
| `mapsplatform.google.com` | 98% | Official product/marketing site |
| `cloud.google.com` | 95% | Maps Platform terms, support hub, status |

All subsequent searches restricted to these 3 domains.

---

## Stage 2 — Break the Claim into Parts (Elements)

**Goal:** What are the individual technical requirements in the claim?

One LLM call reads the full claim text + selected paragraphs of the patent description, then splits the claim into 4–8 elements. Each element is one logical requirement.

**Run13 result (7 elements, from `02_elements.json`):**

| ID | Element | Keywords |
|----|---------|----------|
| E1 | Dispatch terminal receives address + event data, correlates address to location-based data in DB | `dispatch terminal, address lookup, event data, database correlation, location-based data` |
| E2 | Mobile second terminal has map display + obtains its own location via GPS | `mobile terminal, map display, GPS receiver, current location, location system` |
| E3 | Second terminal transmits its location to first terminal over comms channel | `wireless transmission, communications channel, send location data, terminal-to-terminal, uplink` |
| E4 | First terminal stores received location data in memory | `store location data, memory buffer, retain received data, dispatch memory, data storage` |
| E5 | When criteria met, first terminal sends correlated location + event back to second | `criteria-based dispatch, conditional transmission, event trigger, send location data, dispatch criteria` |
| E6 | Second terminal automatically enters received data into display unit (no user action) | `automatic input, no user intervention, auto-populate, GPS input, terminal display input` |
| E7 | Second terminal displays map including the first address | `map display, route map, address display, correlated location, navigation map` |

The patent description biases element labels toward concrete vocabulary the inventor used ("dispatch terminal", "GPS receiver") instead of fully abstract claim language.

---

## Stage 2a — Classify the Use-Case (NEW)

**Goal:** Pin down the technical domain in 2–6 words once, then share it with every downstream stage.

Without this stage, three downstream stages (sub-product probe, rewriter, expander) each independently guessed at the claim's domain from their slice of context. Multi-API umbrella products like Google Maps Platform have ~20 surfaces; if the sub-product probe guesses "geocoding" while the rewriter guesses "navigation", queries diverge and miss real docs.

One LLM call (`agents/use_case.py::UseCaseAgent`) reads the full claim + spec context and emits a single use-case label + a small set of vocabulary anchor tokens.

**Run13 result (`02a_use_case.json`):**

```json
{
  "use_case": "Mobile dispatch mapping",
  "anchors": [
    "dispatch", "GPS receiver", "map display",
    "location based data", "wireless infrastructure", "event codes"
  ],
  "alternative_use_cases": ["fleet routing", "field service dispatch"]
}
```

These anchors are passed to:
- **Sub-product probe** — biases catalogue filtering toward dispatch/fleet surfaces.
- **Query rewriter** — every emitted query must contain at least one anchor (or sub-product name, or product brand).
- **Path-neighborhood expander** — uses anchors as the second query template under hot path prefixes.

Result: every downstream stage targets the same use-case instead of re-deriving it.

---

## Stage 2b — Sub-Product Probe

**Goal:** Google Maps Platform has 20+ APIs. Which ones likely document this dispatch + mobile-location + map-display behaviour?

The probe is **evidence-based** — it never relies on the LLM's memory of the catalogue. Three phases:

### Phase 1 — Catalogue evidence collection

SerpApi probes (parallel, max 5 workers):
- `"Google Maps Platform products list"`
- `"Google Maps Platform all APIs"`
- `"Google Maps Platform documentation index"`
- `"products site:developers.google.com"`
- `"documentation overview site:mapsplatform.google.com"`
- … (~12 probe templates)

These return real catalogue / overview / product-list pages on the official domains.

### Phase 2 — Catalogue page-body fetch

The top 8 catalogue candidates are ranked by `(1 + keyword_score) / (1 + path_depth)` with a +0.25 boost for `developers.*` / `docs.*` / `devdocs.*` subdomains (fix in commit `0da3ae8` after run4: deep paths with many keyword segments were outranking shallow product index pages).

Their bodies are fetched (8000 chars each, raised from 4000 in this iteration so the entire menu fits). Niche surfaces like Fleet Engine, Mobility SDK, On-Demand Rides routinely appear as inline anchors on `mapsplatform.google.com/maps-products/` even when SerpApi never surfaces them as titles.

### Phase 3 — LLM filter (default: single-step)

Default: one combined LLM call with catalogue evidence + claim + use-case anchors → ranks sub-products. The use-case anchors (`"dispatch"`, `"GPS receiver"`, `"map display"`) bias the filter toward surfaces matching the claim's domain.

**Opt-in alternative (`--subproduct-two-step`):** Split into Step A (enumerate every visible sub-product, no relevance filter) → Step B (rank enumeration against claim + use-case). Theoretical popular-API debias. A/B against single-step on test patent showed no measurable top-k gain (run10 single-step = run13 two-step = 5/13). Code retained for future patents where popular-API bias may matter more.

**Run13 result (`02b_subproducts.json`, 8 picked):**

| Sub-product | Why selected |
|-------------|-------------|
| Route Optimization API | Dispatching location + event data, returning routing info — closest match to a dispatch system |
| Route Optimisation agent | Fleet/dispatch vocab aligns with claim's mobile dispatch workflow |
| Navigation SDK for Android | Mobile terminal + automatic location + map display |
| Navigation SDK for iOS | Same, iOS variant |
| Navigation SDK | Umbrella SDK; in-vehicle/mobile navigation after dispatch |
| Routes API | Transmitting location-based data + map display to destination |
| Compute Routes | Vehicle routing + directions overlapping with terminal-to-terminal flow |
| Maps URLs | Automatic map display of received address |

Note: Fleet Engine itself didn't make this run13 list (Fleet Engine surfaces only when the catalogue probe surfaces it as a candidate, which depends on the run's SerpApi snapshot). The path-neighborhood expansion + index-link harvest stages below close that gap by surfacing Fleet Engine pages anyway — Driver SDK pages reach the top 10 with score 0.95.

---

## Stage 3 — Rewrite Elements into Search Queries

**Goal:** Turn patent jargon into Google product vocabulary that SerpApi will find.

The patent says *"remote dispatch terminal receives location-based data"*. No Google doc uses those words. Google docs say *"Geolocation API device location"*, *"Navigation SDK location sharing"*, etc.

One LLM call gets the **full claim text** (not just element labels) + the 8 sub-products + the use-case anchors. It generates **4 search queries per element** (28 total for 7 elements).

### Two new constraints (this iteration)

1. **Anchor rule (mandatory).** Every emitted query MUST contain at least one of:
   - a sub-product / surface name from the list above, or
   - a use-case anchor token, or
   - the product name "Google Maps Platform" or its vendor brand.

   Orphan jargon queries like `"dispatch memory location data"` matched zero results on narrow `site:` filters and wasted SerpApi budget. Now forbidden.

2. **Per-surface query cap.** No single sub-product may absorb more than `ceil(total_queries / num_surfaces)` of the queries. With 28 queries × 8 surfaces, cap = 4. Stops the rewriter from clustering all queries on the most popular sub-product.

### Run13 queries (excerpt from `03_queries.json`)

```
E1 (dispatch terminal + address lookup):
  → "Google Maps Platform Route Optimization API dispatch address lookup"
  → "Route Optimization API event data database correlation"
  → "Maps Platform fleet dispatch location based data"
  → "Route Optimisation agent dispatch criteria address"

E2 (mobile terminal + GPS):
  → "Navigation SDK Android map display GPS receiver"
  → "Navigation SDK iOS current location map display"
  → "Google Maps Platform navigation SDK driver location"
  → "Navigation SDK turn-by-turn map display"

E3 (mobile sends location to dispatch):
  → "Routes API send location data communications channel"
  → "Compute Routes vehicle location transmission"
  → "Google Maps Platform route optimization fleet uplink"
  → "Navigation SDK Android location data upload"

E4 (dispatch stores location):
  → "Route Optimization API store location data memory"
  → "Google Maps Platform dispatch memory location data"
  → "Routes API retain received data"
  → "Compute Routes vehicle data storage"

E5 (conditional dispatch when criteria met):
  → "Route Optimization API dispatch criteria conditional transmission"
  → "Route Optimisation agent event trigger fleet"
  → "Google Maps Platform route optimization conditional send"
  → "Routes API optimization criteria event data"

E6 (auto-display received data):
  → "Navigation SDK automatic input map display"
  → "Navigation SDK Android no user intervention"
  → "Google Maps Platform GPS input map display"
  → "Navigation SDK auto populate destination"

E7 (display map of address):
  → "Maps URLs map display address"
  → "Routes API navigation map destination"
  → "Google Maps Platform map display first address"
  → "Navigation SDK route map address"
```

Every query carries an anchor. No query is just patent jargon. Queries spread across 6 sub-products (Route Optimization, Navigation SDK, Routes API, Compute Routes, Maps URLs, Route Optimisation agent) — none exceeds the cap of 4.

---

## Stage 4 — Search SerpApi

**Goal:** For every (query, domain) pair, get the top 10 URLs from Google.

Each unique query × each official domain → `<query> site:<domain>`.

```
Queries × domains  = 28 × 3 = 84 planned pairs
Unique pairs       = 84 (no in-run duplicates)
SerpApi API calls  = 84
Empty responses    = 7  (queries that SerpApi returned 0 hits for)
URLs collected     = 718 (after deduping on URL across pairs)
```

Disk cache (`./.claim_url_cache/serp/`) makes re-runs zero-cost — identical (query, domain, num) → identical hash → cache hit.

**Example: query `"Google Maps Platform navigation SDK driver location"` on `developers.google.com` returned:**
- `developers.google.com/maps/documentation/navigation/android-sdk/...`
- `developers.google.com/maps/documentation/mobility/driver-sdk/navigation`
- `developers.google.com/maps/documentation/mobility/driver-sdk/on-demand`
- … 10 hits total

Note `mobility/driver-sdk` appears here because the use-case anchor `"driver"` made it into the query — without that anchor the query would have read `"Google Maps Platform navigation SDK location"` and only navigation-tutorial pages would surface.

---

## Stage 4b — Path-Neighborhood Expansion (OPT-IN, default OFF)

`agents/expansion.py::PathNeighborhoodExpander`. Issues follow-up SerpApi queries under hot path prefixes (≥2 hits in the initial search). Up to 12 extra SerpApi calls per run.

**Why default off:** Run13 (with this on) added 28 hits, some on-topic (`mobility/services/capabilities`, `mobility/journey-sharing`) but several off-topic (`docs.cloud.google.com/chronicle/soar/...` SOAR docs). Top-k ended at 5/13 — same as run10 / run4 with this off. Index-link harvest (Stage 5b, free) covers the same niche-sub-tree retrieval problem.

**Flip on with `--path-expansion`** for A/B testing on patents where index pages are sparse / non-existent.

---

## Stage 5 — Fetch Page Bodies (Pass 1)

**Goal:** SerpApi only returns 1–2 sentences of SEO snippet. That snippet often says nothing about the actual feature. Fetching the full page body gives the relevance agent real evidence.

### Adaptive Playwright fallback (NEW)

The default fetcher uses `requests`. Some hosts (e.g. `support.google.com`) detect bots and serve CAPTCHAs → empty bodies.

The fetcher now tracks per-host empty-body stats. If a host crosses **4 consecutive empty bodies** with **≥5 total observations**, it is marked **bot-blocked** and all subsequent fetches for that host are routed through Playwright Chromium for the rest of the run. No CLI flag flip required. Also retries the URL that triggered the threshold, once, through Playwright.

If Playwright isn't installed, the fetcher logs a warning and continues serving empty bodies for the blocked host (graceful degrade).

### Run13 result (`05_pagefetch.json`)

```
Unique URLs fetched:  465
Bodies received:      ~300 (4000–6000 chars each)
Empty bodies:         ~165 (mostly cloud.google.com legacy archive pages — content gated)
```

Per-host stats from this run:
```
developers.google.com:        54 obs, 0 empties, blocked=false
cloud.google.com:             39 obs, 0 empties, blocked=false
codelabs.developers.google.com: 3 obs, 0 empties, blocked=false
docs.cloud.google.com:        255 obs, 0 empties, blocked=false
mapsplatform.google.com:       8 obs, 0 empties, blocked=false
```

(No host triggered the blocked threshold this run because the official Maps Platform domains don't bot-block the requests fetcher. The mechanism kicks in for `support.google.com`-style hosts.)

### Why bodies matter — concrete example

URL: `developers.google.com/maps/documentation/mobility/driver-sdk`

SerpApi snippet (what we had without fetching):
> *"The Driver SDK sends real-time location signals to Fleet Engine, which is a required part of enabling location and routing capabilities in Fleet Engine."*

Page body (first 4000 chars):
> *"…The Driver SDK communicates the driver's current vehicle location and route progress to Fleet Engine. Fleet Engine uses these updates to dispatch new trips to the driver, route the driver, and present the destination address on the in-app navigation display…"*

**Score with snippet only:** likely 0.5
**Score with body (run13):** **0.95** matching E2, E3, E4, E5, E6, E7.

---

## Stage 5b — Index-Page Link Harvest (NEW)

**Problem this fixes:** Catalogue / overview pages list dozens of sibling sub-pages inline as anchor links. SerpApi rarely surfaces those individual leaf pages. Without harvesting, niche leaves stay invisible.

### How it works (`agents/expansion.py::IndexLinkHarvester`)

1. Identify likely index pages from the post-fetch corpus. Heuristics:
   - Path depth in `[1, 3]` segments.
   - Path contains an "index hint" segment (`documentation`, `docs`, `overview`, `index`, `products`, `apis`, `services`, `solutions`, `platform`, `guide`, `reference`, `catalog`).
   - OR body length < 1500 chars + tail segment is an index hint (mostly-nav body).
2. For each index, parse cached raw HTML → extract same-domain anchor `href`s under the index page's path.
3. Same-host only (no cross-subdomain marketing redirects).
4. **Multi-pass.** Pass 1 emits direct children. Pass 2+ re-processes any newly-discovered URL that itself looks like an index → grandchildren of deep index hierarchies are surfaced. Common case: `/docs/` parent → `/docs/foo/` → leaves.
5. Cap: `--index-link-harvest-max-total` (default 200).

The fetcher keeps raw HTML in memory alongside stripped body so this stage requires zero extra HTTP calls. Disk-cached bodies don't carry raw HTML (would bloat cache); for those, a live re-fetch happens via `ensure_raw_html()`.

The sub-product probe's already-fetched catalogue pages (`mapsplatform.google.com/maps-products/`, etc.) are seeded as additional index candidates so the harvester doesn't miss the canonical menu.

### Run13 result

```
Index-page link harvest: 200 new candidates over 2 passes
```

**Examples of harvested URLs:**
- `developers.google.com/maps/documentation/mobility/fleet-engine` ← reference URL D2
- `developers.google.com/maps/documentation/mobility/fleet-engine/essentials` ← reference URL D3
- `developers.google.com/maps/documentation/mobility/services/capabilities/driver-routing` ← reference URL D7
- `developers.google.com/maps/documentation/route-optimization/overview` ← reference URL D11

These all came from harvesting `/maps/documentation/mobility/` and `/maps/documentation/route-optimization/` index pages — pages SerpApi did not return as direct hits.

After harvest, a second page-fetch pass (Stage 5c) populates bodies for the 200 newly-enqueued URLs so they reach the relevance scorer with real content, not just titles.

---

## Stage 6 — Score Each URL (Relevance Agent)

**Goal:** For each URL in the candidate pool, decide which claim elements it evidences and assign a score 0.0–1.0.

URLs are batched (35 at a time, 4 batches in parallel). Each batch goes to the LLM with:
- Full claim text
- All 7 elements (E1–E7) with keywords
- Each URL's title, SerpApi snippet, page body (if fetched)

### Scoring rubric

| Score | Meaning |
|-------|---------|
| 1.0 | Page directly describes product behaviour matching a claim limitation |
| 0.75 | Same feature, different vocabulary |
| 0.5 | Adjacent / supporting — related feature area |
| 0.25 | Weak — mentions topic but doesn't describe limitation |
| 0.0 | Unrelated (dropped) |
| Legal/TOS/policies/pricing pages | Force-scored 0.0 (rule added run9) |

### Run13 result (`06_scoring.json`)

```
Scored count:        300
Above 0.5:           112
Above 0.75:           46
Above 0.9 (top tier): ~20
```

### Top-scored URLs (run13, before post-processing):

| Score | Elements | URL |
|-------|---------|-----|
| 0.95 | E2, E3, E6, E7 | `developers.google.com/maps/documentation/navigation/android-sdk/route` |
| 0.95 | E1, E2, E4, E6, E7 | `cloud.google.com/customers/rapido-maps` |
| 0.95 | E2–E7 | `developers.google.com/maps/documentation/mobility/driver-sdk` ← reference D13 |
| 0.90 | E2, E3 | `codelabs.developers.google.com/codelabs/maps-platform/navigation-sdk-101-android` |
| 0.90 | E1, E2, E3, E4 | `developers.google.com/maps/documentation/navigation/android-sdk/faq` |
| 0.90 | E2–E7 | `developers.google.com/maps/documentation/mobility/driver-sdk/navigation` ← reference D12 |
| 0.90 | E2, E6, E7 | `cloud.google.com/customers/dominos-maps` |
| 0.85 | E2–E7 | `mapsplatform.google.com/maps-products/navigation-sdk/` |
| 0.85 | E1, E3, E4 | `mapsplatform.google.com/solutions/transportation-and-logistics/` |
| 0.85 | E2–E7 | `developers.google.com/maps/documentation/mobility/driver-sdk/on-demand` ← reference D9 |

3 of 13 reference URLs reach the top 10 directly: D9, D12, D13. Fleet Engine canonical pages (D2–D7) reach the scored pool above 0.5 — they're picked up by the element-coverage guard below.

### Why the description is NOT used at this stage

Spec context was tried in the relevance agent (commits `c1c45b2` to `0aef592`) and made results worse: agent became too strict, penalised correct Fleet Engine pages, dropped 7 references to score 0.0. Reverted. See `trace/EXPERIMENTS.md` for the full failure analysis.

---

## Post-Processing — Diversity + Two-Tier Element Coverage

After Stage 6, two filters run before the final top-k cut:

### Diversity guard

Within each tied-score tier (e.g. all URLs at 1.0), bucket by first 4 path segments. Cap each bucket at 3. If 10 URLs share `/maps/documentation/javascript/`, keep 3, push 7 to the bottom of the tier.

URLs with strictly higher scores are never displaced — only ties get reordered. Stops one feature area from filling all 10 slots when many URLs tie.

### Two-tier element coverage (NEW two-tier behaviour)

After top-k cut, check if every element (E1–E7) has at least one URL representing it.

- **Pass 1** (floor `--coverage-score-floor`, default 0.5): for each uncovered element, append the highest-scoring URL above 0.5 that matches it.
- **Pass 2** (floor `--coverage-score-floor-secondary`, default 0.25): for any element still uncovered, relax to 0.25 floor.

**Why two tiers:** Niche / vertical surfaces routinely score 0.25–0.50 when their pages don't have body text yet (bot-blocked hosts, missed by harvester). The secondary pass surfaces them as covering URLs without polluting the headline list with weak-tier matches. Pass 1 stays strict.

Output may slightly exceed top-k.

---

## Run13 Final Output (Top 10 — `07_final.json`)

```
0.95  E2,E3,E6,E7      developers.google.com/maps/documentation/navigation/android-sdk/route
0.95  E1,E2,E4,E6,E7   cloud.google.com/customers/rapido-maps
0.95  E2-E7            developers.google.com/maps/documentation/mobility/driver-sdk           ← REF
0.90  E2,E3            codelabs.developers.google.com/codelabs/maps-platform/navigation-sdk-101-android
0.90  E1,E2,E3,E4      developers.google.com/maps/documentation/navigation/android-sdk/faq
0.90  E2-E7            developers.google.com/maps/documentation/mobility/driver-sdk/navigation ← REF
0.90  E2,E6,E7         cloud.google.com/customers/dominos-maps
0.85  E2-E7            mapsplatform.google.com/maps-products/navigation-sdk/
0.85  E1,E3,E4         mapsplatform.google.com/solutions/transportation-and-logistics/
0.85  E2-E7            developers.google.com/maps/documentation/mobility/driver-sdk/on-demand ← REF
```

3 reference URLs in top 10 (D9, D12, D13). Element coverage is satisfied: every E1–E7 has at least one URL mapping to it.

---

## How the Patent Description Helps

The description feeds **three** stages, all upstream of any URL retrieval:

| Stage | Uses description? | What it changes |
|-------|:-----------------:|-----------------|
| Domain discovery | No | N/A |
| Element extraction (Stage 2) | **Yes** | Better element labels + keywords (fleet/dispatch vocabulary) |
| Use-case classification (Stage 2a) | **Yes** | More accurate use-case label + anchors |
| Sub-product probe (Stage 2b) | **Yes** | Prefers niche surfaces (Fleet Engine) over popular ones (Maps JS) |
| Query rewriting (Stage 3) | **Yes** | Concrete dispatch/driver terms instead of patent jargon |
| SerpApi search | No | Runs whatever Stage 3 produced |
| Page fetch | No | Just HTTP |
| Relevance scoring (Stage 6) | **No** | Tried — made results worse. Reverted. |

### Paragraph selection

Patent descriptions can be 100+ paragraphs of boilerplate, prior art, figure captions. Only a few paragraphs explain the technical implementation relevant to the specific claim.

The tool picks the **top 10 paragraphs by keyword overlap**:
1. Extract meaningful words from claim text (strip stopwords like "a", "the", "comprising", "wherein").
2. Score every description paragraph by how many of those words it contains.
3. Keep the top 10, in original document order.

For Claim 1 of US7629884B2: paragraphs describing dispatch station architecture, driver mobile GPS flow, address transmission, navigation display update. Drops figure-numbering boilerplate.

### Concrete before/after

| Stage | Without description | With description |
|-------|--------------------|--------------------|
| E1 keywords | `["address lookup", "location data", "event data"]` | `["dispatch terminal", "address lookup", "event data", "database correlation", "location-based data"]` |
| E1 query 1 | `"Maps JavaScript API address lookup"` | `"Google Maps Platform Route Optimization API dispatch address lookup"` |
| E1 query 2 | `"Geocoding API event data"` | `"Route Optimization API event data database correlation"` |
| Sub-product top pick | Geocoding API | Route Optimization API |

---

## Why So Many Stages?

Each active stage exists because a previous run revealed a specific failure. Read `trace/EXPERIMENTS.md` for the run-by-run history.

| Stage | Default | Failure it fixes / status |
|-------|:-------:|---------------------------|
| 2a Use-case classifier | ON | Downstream stages re-derived domain independently → divergence. Provides anchor tokens for rewriter rule. |
| 3 Anchor rule + per-surface cap | ON | Orphan jargon queries returned zero hits; queries clustered on most popular surface. |
| 4b Path-neighborhood expansion | **OFF** | Theoretical niche-sub-tree retrieval. Overlapped with index harvest, leaked off-topic. Code retained, opt-in via `--path-expansion`. |
| 5 Adaptive Playwright | ON | Bot-blocked hosts returned empty bodies → 0 evidence for scorer. |
| 5b Index-page link harvest | ON | Catalogue index pages list 50+ leaves inline that SerpApi never returned. **Single biggest win this iteration. Zero SerpApi cost.** |
| 2b Sub-product two-step | **OFF** | Theoretical popular-API debias. No measurable top-k gain (5/13 either way). Code retained, opt-in via `--subproduct-two-step`. |
| Diversity guard | ON | One feature area drowned the top-k when many URLs tied. |
| Two-tier coverage | ON | Niche surfaces with weak titles needed 0.25-floor fallback to be representable. |

---

## Search Budget (default config, run13-like)

```
Domain probes (Stage 1):       5 SerpApi calls
Sub-product probes (Stage 2b): ~12 SerpApi calls + 8 page fetches
Main search (Stage 4):         84 SerpApi calls
Page fetch pass 1:             465 HTTP fetches
Page fetch pass 2 (harvested): 200 HTTP fetches
─────────────────────────────────────────────
Total SerpApi calls:           ~101
Total HTTP page fetches:       ~673
LLM calls:
  Domain classify:               1
  Element extract:               1
  Use-case classify:             1
  Sub-product filter:            1   (single-step default)
  Query rewrite:                 1
  Relevance score:              ~9 batches
  ─────────────────────────────────
  Total LLM calls:             ~14
```

If both opt-in mechanisms enabled (`--path-expansion --subproduct-two-step`): +1 LLM call, +12 SerpApi calls per run.

Disk cache (`./.claim_url_cache/`) makes re-runs near-zero-cost: every SerpApi call, every LLM completion at temperature 0.0, every page body keyed by sha256(input) → JSON file.

---

## Complete Flow Diagram (default config)

```
INPUT
  Patent: US7629884B2, Claim 1
  Product: Google Maps Platform
       │
       ▼
[1] DOMAIN DISCOVERY
    5 SerpApi probes → LLM classifies domains
    → developers.google.com, mapsplatform.google.com, cloud.google.com
       │
       ▼
[2] CLAIM ELEMENT EXTRACTION
    LLM reads claim + selected description paragraphs → 7 elements (E1–E7)
       │
       ▼
[2a] USE-CASE CLASSIFICATION
    LLM → "Mobile dispatch mapping"
    Anchors: dispatch, GPS receiver, map display, ...
       │
       ▼
[2b] SUB-PRODUCT PROBE (single-step default)
    SerpApi catalogue probes → fetch top 8 catalogue pages (8000 chars)
    Single LLM filter call: claim + use-case anchors → ranks surfaces
    → 8 surfaces: Route Optimization, Navigation SDK Android/iOS, ...
    (Opt-in `--subproduct-two-step` splits into enumerate + filter)
       │
       ▼
[3] QUERY REWRITING
    LLM: patent jargon → product vocabulary
    Anchor rule: every query has surface OR anchor OR product brand
    Per-surface cap: ceil(28/8) = 4
    → 4 queries × 7 elements = 28 queries
       │
       ▼
[4] SERPAPI SEARCH
    28 queries × 3 domains = 84 API calls → 718 raw hits
    (Opt-in `--path-expansion` adds up to 12 follow-up SerpApi calls)
       │
       ▼
[5] PAGE FETCH (pass 1)
    Parallel HTTP / Playwright; adaptive fallback for bot-blocked hosts
    → 465 unique URLs, ~300 with body, raw HTML kept in memory
       │
       ▼
[5b] INDEX-PAGE LINK HARVEST
    Parse cached HTML on index pages → enqueue same-domain children
    Two-pass: index → sub-index → leaves
    Seeded with sub-product catalogue pages
    → +200 candidate URLs (Fleet Engine essentials, trip-details, ...)
       │
       ▼
[5c] PAGE FETCH (pass 2)
    Fetch bodies for the 200 newly-harvested URLs
       │
       ▼
[6] RELEVANCE SCORING
    LLM scores each URL 0.0–1.0 (batches of 35, 4 in parallel)
    Uses: claim text + 7 elements + page body (description NOT injected)
    Legal/TOS pages forced to 0.0
    → 300 scored above 0.0; 112 above 0.5; 46 above 0.75
       │
       ▼
[POST] DIVERSITY GUARD + TWO-TIER COVERAGE
    Within tied-score tiers, cap per path-prefix at 3
    Pass 1 (floor 0.5): cover any missing element
    Pass 2 (floor 0.25): relax for niche surfaces
       │
       ▼
OUTPUT (top 10)
    Run13 result: 5/13 reference URLs hit (D2, D7, D9, D12, D13)
    Pool recall ceiling: 13/13 (100%) across the 300 scored
    Bottleneck: scorer ranking, not retrieval
```
