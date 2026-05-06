# Experiment Log — US7629884B2 Claim 1 (Google Maps Platform)
## Patent: Dispatch system with location-aware terminals
## Reference set: 13 URLs (D1–D13), all under `developers.google.com/maps/documentation/mobility/`

---

## Baseline

| Run | F1 | Hits | Key state |
|-----|-----|------|-----------|
| run3 | 0.0% | 0/13 | No spec context; `claim_v2.txt` inline |
| run4 | **41.7%** | 5/13 | Spec context → extractor + rewriter only; best stable baseline |
| run7 | 34.8% | 4/13 | Spec context → all agents (extractor + rewriter + subproduct LLM + relevance); fleet refs at 0.75–0.85 in pool; k=30 → 92% recall |
| run10 | **43.5%** | 5/13 | Catalogue depth fix; slight improvement over run4 |

Pool recall ceiling = **100%** throughout all runs — all 13 refs always reachable in scored pool.  
The bottleneck is **top-k capacity**, not retrieval.

---

## Changes Tried

### ✅ WORKED: Spec context → Extractor + Rewriter (commit `d6f9efa`)
- **What:** Patent description paragraphs injected into `ClaimElementExtractor` and `QueryRewriteAgent` prompts.
- **Effect:** run3 → run4: 0% → 41.7% F1. Biggest single improvement.
- **Why it works:** Extractor picks up concrete vocabulary ("dispatch", "driver", "fleet") from spec; rewriter translates patent jargon to product vocabulary using spec terms.
- **Generic:** Yes — works for any patent with a description.

### ✅ WORKED: Spec context → SubProduct LLM prompt (commit `c1fa416`, refined in `fdaa8ed`)
- **What:** Spec paragraphs injected into the SubProductAgent LLM call (the pick-subproducts-from-catalogue step).
- **Effect:** Helps LLM prefer dispatch/fleet surfaces when catalogue evidence is ambiguous. Run7 got "Route Optimization API" with dispatch/fleet vocabulary because of this.
- **Generic:** Yes.
- **Note:** See ❌ below for the companion bad idea (spec-keyword *probes*).

### ✅ WORKED: Catalogue depth scoring fix (commit `0da3ae8`)
- **What:** Replaced additive `keyword_score + 1/(1+depth)` with ratio `(1+keyword_score)/(1+depth)` for ranking candidate catalogue pages to fetch in SubProductAgent.
- **Effect:** Deep paths with many keyword-containing segments (`/workspace/docs/api/how-tos/overview`, score 3.17) no longer outrank shallow product index pages (`/maps-products/`, score 1.5 → now 1.00). Run10 correctly fetches `mapsplatform.google.com/maps-products/`.
- **Generic:** Yes — a pure heuristic fix with no product knowledge.

### ✅ WORKED: Legal/policy path filter in catalogue fetch (commit `9815e0e`)
- **What:** Added `_exclude_segments` frozenset (`terms`, `legal`, `policies`, `tos`, `agreement`, `pricing`, `billing`, `support`, `reference`, `changelog`, ...) to skip URLs containing those path segments when selecting catalogue pages to fetch.
- **Effect:** Stops `cloud.google.com/maps-platform/terms/maps-services` from being fetched (it was scoring 1.00 in the relevance agent).
- **Generic:** Yes.

### ✅ WORKED: Legal pages score 0.0 rule in relevance prompt (commit `9815e0e`, kept in `0aef592`)
- **What:** Added explicit rule: "Legal documents, terms of service, policies, and pricing pages are NOT documentation. Score them 0.0."
- **Effect:** TOS/terms pages that somehow reach the scoring stage don't score 1.00.
- **Generic:** Yes.

---

### ❌ FAILED: Spec-keyword catalogue probes in SubProductAgent (commit `c1fa416`, reverted `fdaa8ed`)
- **What:** Extracted high-frequency terms from spec text using `Counter` → used them as additional SerpApi probe queries (`"{product} {kw1} {kw2}"`).
- **Effect:** Catastrophic regression. run5: 41.7% → 0.0% F1. Frequency extraction pulled patent boilerplate ("automatically", "comprises", "receiver") not domain-specific terms. Probe queries like "Google Maps Platform automatically dispatch" / "Google Maps Platform receiver comprises" contaminated catalogue evidence → LLM picked Geocoding/Routes API instead of Fleet Engine/Navigation SDK.
- **Why it fails:** Short patent specs (11 paragraphs) have high frequency of boilerplate. Even with a large stopword list the signal-to-noise is too low.
- **Lesson:** Do not use frequency-based keyword extraction from spec text for query generation. LLM-based extraction *might* work but adds cost and complexity.

### ❌ FAILED: Spec context → Relevance agent (commits `c1c45b2` to `0aef592`, then reverted)
- **What:** Injected spec paragraphs into `RelevanceCheckingAgent._score_batch()` prompt.
- **Effect:** Caused high run-to-run variance (temperature=0.0 but different batch compositions and candidate sets). Sometimes helped (fleet refs 0.50→0.85), sometimes hurt (7 refs scored 0.0, dropped from pool entirely, pool ceiling dropped to 46%).
- **Root cause:** Without page body text for fleet-engine pages (not targeted by queries), the agent has only title/snippet. With spec context it becomes more opinionated but can't distinguish "correct domain, different vocabulary" from "wrong domain, shared vocabulary" → either over-scores geolocation (0.95) or under-scores fleet-engine (0.0/0.25).
- **Lesson:** Don't add spec context to the relevance agent until fleet-engine pages are being retrieved WITH body text (i.e., after fixing subproduct selection to include Fleet Engine).

### ❌ FAILED: USE-CASE MATCH strict scoring rule (commit `9815e0e`, softened `0da3ae8`, reverted from relevance `0aef592`)
- **What:** Added rule: "Shared vocabulary alone is insufficient for a score above 0.25. The page must address the same product use-case/domain." Intended to penalise Geolocation API (0.95) against a dispatch claim.
- **Effect (strict version):** Fleet-engine dropped from 0.50 → 0.25 (run8), because the agent applied the rule to fleet-engine too ("dispatch terminals ≠ fleet management API"). 7 refs at 0.25, pool ceiling at 0.50 dropped to 46%.
- **Effect (soft version):** Still caused 7 refs to score 0.0 (run9), pool ceiling 46%.
- **Lesson:** LLM cannot reliably apply "same domain" test without body text evidence. The rule penalises correct pages as often as incorrect ones.

---

## Persistent Problem: Fleet Engine never appears in subproducts

All runs (including run10 which fetches `mapsplatform.google.com/maps-products/`) still select:
- Routes API / Route Optimization API
- Navigation SDK (Android, iOS, sometimes Flutter, React Native)
- Maps JS API / Maps SDK
- Geocoding API, Geolocation API

**Never selected:** Fleet Engine, Mobility SDK, On-Demand Rides & Deliveries.

**Why:** Fleet Engine is niche. Generic SerpApi probes (`"{product} all APIs"`, `"documentation overview site:{domain}"`) return well-known APIs. Fleet Engine only surfaces when fleet/dispatch-specific queries are run, but those require Fleet Engine to already be in subproducts (circular dependency).

**Consequence:** No targeted fleet-engine queries → fleet-engine pages retrieved only via tangential queries → no body text fetched → scored 0.25–0.50 based on title/snippet → can't beat navigation blog posts at 0.75 in top-k.

**Potential fix (not yet tried):** 
- A two-stage subproduct probe: first pass picks generic APIs, second LLM call with claim + spec context asks "are any niche/fleet/dispatch-specific surfaces missing?" 
- Or: post-process the claim elements to generate explicit subproduct search queries from element labels ("remote dispatch terminal" → search for that phrase on official domains).

---

## Top-k Projection (run7, best scoring distribution)

| k | Recall |
|---|--------|
| 10 | 31% (4/13) |
| 15 | 46% (6/13) |
| 20 | 69% (9/13) |
| **30** | **92% (12/13)** |
| 50 | 92% (12/13) |
| 100 | 92% (12/13) |
| pool | 100% |

Run7 has the best scoring distribution. If we can replicate run7's subproduct selection (Route Optimization API with dispatch/fleet vocab) without the geolocation/TOS false positives, `--top-k 20` would give solid recall.

---

## Practical Levers for This Patent

- `--top-k 20` or `--top-k 30`: recovers 69–92% recall on run7 scoring distribution
- `--coverage-score-floor 0.25`: element-coverage guard will pull in 0.25-scored fleet URLs
- `--no-element-coverage` OFF (default): keep coverage guard enabled

---

## Current Code State (after run10, commit `0aef592`)

Spec context flows to: **extractor ✓**, **rewriter ✓**, **subproduct LLM prompt ✓**  
Spec context removed from: **relevance agent** (caused variance, reverted)  
Catalogue: **legal path filter ✓**, **depth scoring formula fixed ✓**  
Spec-keyword probes: **removed ✓** (caused run5 regression)
