# How the URL Finder Works — End to End

**Example used throughout:** `--product "Google Maps Platform" --patent "US7629884B2" --claim-number 1`

---

## What You Give It

```
Product:      Google Maps Platform
Patent:       US7629884B2
Claim number: 1
```

The tool fetches the claim text and patent description from the patent database, then finds official documentation URLs that are evidence for each sentence in the claim.

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

## Stage 1 — Find the Official Domains

**Goal:** What websites does Google Maps Platform officially use?

The tool runs 5 SerpApi searches:
- `"Google Maps Platform official website"`
- `"Google Maps Platform official support"`
- `"Google Maps Platform documentation official"`
- ... etc.

It collects all URLs returned, then asks an LLM: *"Which of these domains are officially owned by Google Maps Platform?"*

**Result from this run:**

| Domain | Confidence | Why |
|--------|-----------|-----|
| `developers.google.com` | 99% | Official developer docs and API references |
| `mapsplatform.google.com` | 98% | Official product/marketing site |
| `support.google.com` | 93% | Google Help Center (Maps help articles) |

All subsequent searches are restricted to these 3 domains.

---

## Stage 2 — Break the Claim into Parts (Elements)

**Goal:** What are the individual technical requirements in the claim?

One LLM call reads the full claim text and splits it into 4–8 elements. Each element is one logical requirement.

**Result:**

| ID | Element (what the claim requires) | Keywords |
|----|----------------------------------|----------|
| E1 | Dispatch terminal receives address + event data, looks it up in location database | dispatch terminal, address lookup, event data, database correlation |
| E2 | Mobile device has a map display and knows its own GPS location | mobile terminal, map display, GPS receiver, current location |
| E3 | Mobile sends its location to the dispatch terminal over a comms channel | wireless transmission, send location data, mobile to dispatch |
| E4 | Dispatch terminal stores the mobile's location in memory | store in memory, location data storage |
| E5 | When conditions are met, dispatch terminal sends the address back to the mobile | criteria met, conditional transmission, dispatch response |
| E6 | Mobile automatically shows the address on its map display | automatic input, map display update, destination address |

---

## Stage 3 — Find Relevant Sub-Products (Sub-Product Probe)

**Goal:** Google Maps Platform has 20+ APIs. Which ones are most likely to document this dispatch/location behaviour?

The tool probes SerpApi with queries like:
- `"Google Maps Platform products list"`
- `"Google Maps Platform all APIs"`
- `"products site:developers.google.com"`
- `"documentation overview site:mapsplatform.google.com"`

It fetches the body text of the top catalogue pages (e.g. `mapsplatform.google.com/maps-products/`) and reads which APIs are listed there.

Then one LLM call asks: *"Given the claim is about dispatch + mobile location + map display, which of these sub-products are relevant?"*

**Result (8 sub-products selected):**

| Sub-product | Why selected |
|-------------|-------------|
| Routes API | Dispatch involves routing and directions |
| Navigation SDK for Android | Mobile terminal with map display and turn-by-turn |
| Navigation SDK for iOS | Same, iOS version |
| Maps JavaScript API | Web-based map display |
| Maps Embed API | Simple map display of an address |
| Geocoding API | Converting an address to coordinates (E1) |
| Geolocation API | Mobile getting its own location (E2) |
| Maps SDK for Android | Mobile map display with location |

These sub-products are passed to the next stage so the query writer can generate searches targeting each one.

---

## Stage 4 — Rewrite Elements into Search Queries

**Goal:** Turn patent jargon into Google product vocabulary that SerpApi will find.

The patent says *"remote dispatch terminal receives location-based data"*. No Google doc uses those words. Google docs say things like *"Geolocation API device location"* or *"Navigation SDK location sharing"*.

One LLM call gets the claim text + the 8 sub-products and generates **4 search queries per element** (24 queries total).

**Result — queries generated:**

```
E1 (dispatch terminal + address lookup):
  → "Routes API geocode address"
  → "Geocoding API address lookup"
  → "dispatch location data geocoding"
  → "Maps JavaScript API address marker"

E2 (mobile device + own GPS location):
  → "Navigation SDK for Android current location"
  → "Navigation SDK for iOS turn-by-turn"
  → "Maps SDK for Android map display"
  → "Geolocation API device location"

E3 (mobile sends location to dispatch):
  → "Geolocation API send location"
  → "Navigation SDK for Android location sharing"
  → "mobile location transmission"
  → "Routes API travel time"

E4 (dispatch terminal stores location):
  → "dispatch memory location data"
  → "store location data"
  → "Maps SDK for Android markers"
  → "Geolocation API coordinates"

E5 (conditional: send address back when criteria met):
  → "Routes API route optimization"
  → "conditional transmission event data"
  → "Navigation SDK for iOS route guidance"
  → "Maps Embed API interactive map"

E6 (mobile auto-displays address on map):
  → "Maps JavaScript API interactive maps"
  → "Maps Embed API map display"
  → "Navigation SDK for Android automatic input"
  → "Maps SDK for Android destination marker"
```

---

## Stage 5 — Search SerpApi

**Goal:** For every (query, domain) pair, get the top 10 URLs from Google.

Each query is run against each of the 3 official domains using `<query> site:<domain>`.

Example:
- `"Geocoding API address lookup" site:developers.google.com`
- `"Geocoding API address lookup" site:mapsplatform.google.com`
- `"Geocoding API address lookup" site:support.google.com`

**Stats from this run:**

```
Query + domain pairs planned:  72
Unique pairs dispatched:       72
SerpApi API calls made:        72
Empty responses (no results):   5
URLs collected total:         646
```

**Example: query `"dispatch location data geocoding"` on `developers.google.com` returned:**
- `developers.google.com/maps/documentation/route-optimization/overview`
- `developers.google.com/maps/documentation/routes/compute-route-matrix-over`
- `developers.google.com/maps/documentation/places/web-service/place-id`
- `developers.google.com/maps/documentation/routes/route-usecases`

All 646 URLs go to the next stage.

---

## Stage 6 — Fetch Page Bodies

**Goal:** SerpApi only returns a short snippet (1–2 sentences of SEO text). That snippet often says nothing about the actual feature. Fetching the full page body gives the scoring agent real evidence.

The tool makes HTTP requests to each unique URL and extracts the first **4,000 characters** of readable text (HTML tags stripped).

**Stats from this run:**

```
Unique URLs to fetch: 434
Fetched with body:    263  (4,000 chars each)
Returned empty:       171  (all support.google.com pages → Google blocks bots)
```

**Why support.google.com returns empty:**
Google detects automated requests on `support.google.com` and returns a CAPTCHA page. The plain HTTP fetcher gets 0 bytes of useful content for all 171 support.google.com URLs. The `--playwright-fetch` flag uses a real Chromium browser to bypass this.

**Example — what the page body adds:**

URL: `developers.google.com/maps/documentation/android-sdk/examples/my-location`

SerpApi snippet (what we had without fetching):
> *"Maps SDK for Android lets you add location-aware features..."*

Page body text (first 4,000 chars):
> *"...enabling My Location layer...the blue dot shows the device's current position...tapping the button re-centers the camera on the device's location...the location data is exposed via the FusedLocationProviderClient..."*

**Score without body:** likely 0.5 (snippet too generic)
**Score with body:** 0.95 (body clearly describes mobile location display on map)

---

## Stage 7 — Score Each URL (Relevance Agent)

**Goal:** For each of the 434 unique URLs, decide which claim elements it evidences and assign a score 0.0–1.0.

URLs are batched (35 at a time). Each batch goes to the LLM with:
- The full claim text
- All 6 elements (E1–E6) with their keywords
- Each URL's: title, SerpApi snippet, page body (if fetched)

The LLM scores each URL:

| Score | Meaning |
|-------|---------|
| 1.0 | Page directly describes product behaviour matching a claim limitation |
| 0.75 | Same feature, different vocabulary |
| 0.5 | Adjacent/supporting — related feature area |
| 0.25 | Weak — mentions the topic but doesn't describe the limitation |
| 0.0 | Unrelated (dropped) |

**226 URLs scored above 0.0. Top results:**

| Score | Elements | URL |
|-------|---------|-----|
| 1.00 | E2, E3, E4 | `developers.google.com/maps/documentation/geolocation/overview` |
| 1.00 | E2, E3, E4 | `developers.google.com/maps/documentation/geolocation/requests-geolocation` |
| 0.95 | E2, E3, E6 | `developers.google.com/maps/documentation/android-sdk/examples/my-location` |
| 0.95 | E2–E6 | `developers.google.com/maps/documentation/mobility/driver-sdk` |
| 0.95 | E1–E3, E5, E6 | `developers.google.com/maps/solutions/product-locator/best-practices` |

**Example — what the LLM said about the top-scoring URL:**

URL: `developers.google.com/maps/documentation/geolocation/overview`
Score: **1.0**
Rationale: *"Official Geolocation API overview explains determining a device's location from cell towers/Wi-Fi and returning location data, which maps directly to mobile location acquisition (E2) and transmission to a server (E3, E4)."*

---

## Post-Processing — Diversity + Element Coverage

After scoring, two filters run before the final top-k cut:

**Diversity guard:** If 10 URLs share the same path prefix (e.g. all under `/documentation/geolocation/`), cap that prefix at 3. Prevents one API's docs from filling all 10 slots.

**Element coverage:** After taking the top 10, check if every element (E1–E6) has at least one URL representing it. If E5 has no URL in the top 10, append the highest-scoring URL that matches E5 (even if score is below top-10 threshold).

---

## Final Output (Top 10 URLs)

```
1.00  E2,E3,E4      developers.google.com/maps/documentation/geolocation/overview
1.00  E2,E3,E4      developers.google.com/maps/documentation/geolocation/requests-geolocation
0.95  E2,E3,E6      developers.google.com/maps/documentation/android-sdk/examples/my-location
0.95  E2,E3,E4,E5,E6 developers.google.com/maps/documentation/mobility/driver-sdk
0.95  E1,E2,E3,E5,E6 developers.google.com/maps/solutions/product-locator/best-practices
0.90  E3,E4,E5      developers.google.com/maps/documentation/mobility/driver-sdk/on-demand
0.90  E1,E2,E3,E5,E6 developers.google.com/maps/solutions/store-locator/best-practices
0.85  E1,E2,E3,E5,E6 developers.google.com/codelabs/maps-platform/full-stack-store-locator
0.80  E2            developers.google.com/maps/documentation/android-sdk/current-place-tutorial
0.80  E2,E6         codelabs.developers.google.com/codelabs/maps-platform/navigation-sdk-101-android
```

---

## How the Patent Description Helps

The patent has two parts:
1. **Claims** — the legal requirements (what we want to chart).
2. **Description** — paragraphs explaining the invention in plain English, with implementation details and concrete vocabulary.

**The problem without description:**

The claim uses patent jargon: *"remote dispatch terminal"*, *"location-based data"*, *"communications channel"*. The LLM only seeing the claim doesn't know this is a **driver dispatch system**. It might search for generic "location" or "address" APIs.

**What the description adds:**

The description of US7629884B2 mentions:
- Taxi/delivery dispatch scenarios
- Driver mobile devices
- Fleet management
- Real-time vehicle tracking

The tool extracts the most relevant paragraphs from the description and feeds them to:

| Stage | What description helps with |
|-------|----------------------------|
| **Element Extractor** | Picks up vocabulary like "dispatch", "driver", "fleet" → produces better element labels and keywords |
| **Sub-Product Probe** | LLM understands this is fleet/dispatch → prefers Fleet Engine, Mobility SDK over generic APIs |
| **Query Rewriter** | Uses description terms to write fleet-specific queries: *"Navigation SDK driver dispatch"* instead of *"location-based data communications"* |

**Concrete example:**

Without description:
- E1 keywords: `["address lookup", "event data", "location data"]`
- Queries generated: `"Geocoding API address lookup"`, `"Maps JavaScript API event data"`

With description (describing fleet dispatch):
- E1 keywords: `["dispatch terminal", "driver dispatch", "fleet address", "event data"]`
- Queries generated: `"Navigation SDK driver dispatch"`, `"Fleet Engine location"`, `"Geocoding API dispatch address"`

The description bridges the gap between patent language and product documentation language.

---

## Known Limitation — The Fleet Engine Problem

The 13 "ground truth" reference URLs for this patent are all Fleet Engine and Mobility SDK pages under `developers.google.com/maps/documentation/mobility/`.

Fleet Engine rarely appears in the sub-product list because:
1. Generic probes (`"Google Maps Platform products list"`) surface well-known APIs (Maps JS, Geocoding, Navigation SDK).
2. Fleet Engine is niche — it only appears in fleet-specific searches.
3. But fleet-specific searches require Fleet Engine to already be in the sub-product list.
4. Circular dependency: need Fleet Engine to find Fleet Engine.

**Current result:** Fleet Engine does appear in the scored pool (score 0.95) because some queries accidentally retrieve it — but it doesn't get into the final top 10 because Geolocation API pages score 1.0 and fill the slots first.

**Fix being explored:** A second sub-product probe pass that explicitly asks *"are there niche/vertical-specific surfaces missing from this list?"* using the claim + description as context.

---

## Complete Flow Diagram

```
INPUT
  Patent: US7629884B2, Claim 1
  Product: Google Maps Platform
       │
       ▼
[1] DOMAIN DISCOVERY
    5 SerpApi probes → LLM classifies domains
    Result: developers.google.com, mapsplatform.google.com, support.google.com
       │
       ▼
[2] CLAIM ELEMENT EXTRACTION
    LLM reads claim + description → 6 elements (E1–E6)
    Description helps: "dispatch", "driver", "fleet" vocabulary added
       │
       ▼
[3] SUB-PRODUCT PROBE
    SerpApi catalogue probes → fetch mapsplatform.google.com/maps-products/
    LLM reads page body → picks 8 relevant APIs
    Description helps: prefers fleet/dispatch surfaces
       │
       ▼
[4] QUERY REWRITING
    LLM: patent jargon → product vocabulary
    4 queries × 6 elements = 24 queries
    Description helps: uses concrete dispatch/driver terms
       │
       ▼
[5] SERPAPI SEARCH
    24 queries × 3 domains = 72 API calls
    Each returns up to 10 URLs
    Result: 646 raw URLs
       │
       ▼
[6] PAGE FETCH
    HTTP GET each unique URL (434 unique)
    Strip HTML → first 4,000 chars of text
    Result: 263 with body, 171 empty (support.google.com blocked)
       │
       ▼
[7] RELEVANCE SCORING
    LLM scores each URL 0.0–1.0
    Uses: claim text + elements + page body
    Result: 226 URLs scored above 0.0
       │
       ▼
[POST] DIVERSITY + COVERAGE
    Cap per path-prefix → prevent one API dominating top-10
    Append missing element coverage if needed
       │
       ▼
OUTPUT
    Top 10 URLs with scores + which elements each covers
```
