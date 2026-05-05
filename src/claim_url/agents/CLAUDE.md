# src/claim_url/agents/ — pipeline stages

Five files, one per stage. `finder.py` (one level up) wires them together.

## Files

```
domain.py      # Agent 1: DomainIdentificationAgent — discover vendor/official domains
extractor.py   # ClaimElementExtractor — decompose claim into 4–8 ClaimElement (deterministic, not autonomous)
rewriter.py    # QueryRewriteAgent — patent-ese → product-ese; --queries-per-element queries per element
search.py      # OfficialDomainSearch + SearchSummary — site:domain SerpApi calls + filter
relevance.py   # Agent 2: RelevanceCheckingAgent — batch-score each URL 0.0–1.0
```

## Stage-specific notes

### Agent 1 — `domain.py`
- Probe queries: `{product} official website`, `... official support`, etc. (currently 5 — `DOMAIN_PROBE_QUERIES`).
- Evidence collected first, then LLM classifies which domains are vendor-owned.
- Replaces any hardcoded product→domain map. To skip, pass `--domains` at the CLI.

### Extractor — `extractor.py`
- Deterministic extractor wrapping a single LLM call; not an autonomous agent.
- Produces `ClaimElement(id, label, keywords)`. Target 4–8 elements per claim.

### Rewriter — `rewriter.py`
- **Load-bearing for recall**. Without this stage, raw patent vocabulary returns near-zero hits on narrow `site:` searches.
- Translates jargon ("incremental keystrokes", "build string", "error model") → user-facing vocabulary ("search suggestions", "autocomplete", "recommendations").
- Generates `--queries-per-element` queries per element (default 3).
- Falls back to keyword-only query on LLM failure — never blocks the pipeline.

### Search — `search.py`
- For each (rewritten query, domain) pair runs SerpApi `<query> site:<domain>`.
- **In-method cache** dedupes identical (query, domain) pairs to a single SerpApi call per run.
- `_filter_results` accepts a hit only if `utils.domain_matches(hit_domain, target)` is true (exact / subdomain / parent).
- Optional `--exclude-url-patterns` regex blocklist drops obvious non-doc paths.

### Agent 2 — `relevance.py`
- Receives **the full claim text** AND the decomposed elements. The decomposition alone loses context; full claim lets the model make associative jumps ("recommendations" ↔ "presenting most likely items") that the strict per-element rubric otherwise rejects.
- Batches candidate hits (default 35 per batch).
- Recall-first prompt: borderline → 0.25, not 0.0.
- **Dedupe** in `_dedupe`: same URL across batches → higher score wins; tied scores merge `matched_elements` and concatenate rationales.

## Search budget (per run)

```
SerpApi calls ≈ len(DOMAIN_PROBE_QUERIES)                              # Agent 1, currently 5
              + len(domains) * len(elements) * queries_per_element     # search stage
              − duplicate (query, domain) pairs eliminated by cache
```

Default 3 × 8 × 3 ≈ 72 calls + 5 probes. Watch quota when raising `--max-domains`, `--queries-per-element`, or claim length (more elements).

## Page fetch is load-bearing for precision

SerpApi snippets are short SEO blurbs. Pages whose body actually describes the feature were scored 0.0 from the snippet alone but 0.95 with `--fetch-pages` (e.g. `support.google.com/youtubetv/answer/7271625` — "Recommendations on YouTube TV"). Use `--fetch-pages` for production charting; latency cost is N HTTP requests parallelized over `--fetch-workers`.
