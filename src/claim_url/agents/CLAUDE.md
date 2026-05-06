# src/claim_url/agents/ — pipeline stages

Six files. `finder.py` (one level up) wires the five pipeline stages; `product.py` is a CLI-side helper that runs only when `--product` is omitted.

## Files

```
domain.py       # Agent 1: DomainIdentificationAgent — discover vendor/official domains (parallel SerpApi probes)
extractor.py    # ClaimElementExtractor — decompose claim into 4–8 ClaimElement (deterministic, not autonomous)
subproduct.py   # SubProductAgent — map claim to relevant sub-products / feature surfaces of {product}
rewriter.py     # QueryRewriteAgent — patent-ese → product-ese; --queries-per-element queries per element
search.py       # OfficialDomainSearch + SearchSummary — site:domain SerpApi calls (parallel) + filter
relevance.py    # Agent 2: RelevanceCheckingAgent — batch-score each URL 0.0–1.0 (parallel batches)
product.py      # ProductSuggestionAgent — used by CLI when --product is missing
```

## Concurrency model

I/O-bound stages dispatch through bounded `ThreadPoolExecutor`s. Worker counts come from CLI flags:

| Stage | Flag | Default |
|---|---|---|
| Agent 1 probes | `--domain-workers` | 5 |
| Search (per unique `(query, domain)`) | `--search-workers` | 8 |
| Agent 2 batches | `--score-workers` | 4 |
| Page fetch | `--fetch-workers` | 8 |

`LLMClient.usage` is guarded by `threading.Lock`. Providers return `(text, prompt_tokens, completion_tokens)` instead of stashing the counts on a per-instance `last_usage` attribute — required so concurrent calls don't race on the shared field.

## Stage-specific notes

### Agent 1 — `domain.py`
- Probe queries: `{product} official website`, `... official support`, etc. (currently 5 — `DOMAIN_PROBE_QUERIES`).
- Probes run in parallel via `ThreadPoolExecutor(max_workers=--domain-workers)`.
- Evidence collected first, then LLM classifies which domains are vendor-owned.
- Replaces any hardcoded product→domain map. To skip, pass `--domains` at the CLI.

### Product suggestion — `product.py`
- Not part of `ClaimURLFinder.run`. Invoked by `cli._resolve_product` only when `--product` is missing.
- One LLM call: claim → 3–7 named commercial products with vendor + rationale.
- CLI prints a numbered menu; user picks an index, types `c` for a custom name, or types a product name directly.
- Skipped (errors out) when stdin is non-interactive — pass `--product` explicitly in scripts/CI.

### Extractor — `extractor.py`
- Deterministic extractor wrapping a single LLM call; not an autonomous agent.
- Produces `ClaimElement(id, label, keywords)`. Target 4–8 elements per claim.

### Sub-product probe — `subproduct.py`
- Optional stage between Extractor and Rewriter. Default ON; disable with `--no-subproduct-probe`.
- One LLM call: maps the **full claim** onto the sub-products / APIs / SDKs / feature surfaces of `{product}` whose docs are most likely to evidence the claim's limitations.
- Output is a list of `SubProduct(name, vocabulary, rationale)` consumed by the rewriter to (a) seed product-feature vocabulary and (b) force per-surface query coverage.
- Generic — no product-specific hardcoding. Works for any umbrella product (Google Maps Platform, AWS, Salesforce, …).
- Failure (invalid JSON, LLM error) returns an empty list; rewriter degrades gracefully to its default behaviour.

### Rewriter — `rewriter.py`
- **Load-bearing for recall**. Without this stage, raw patent vocabulary returns near-zero hits on narrow `site:` searches.
- Receives the **full claim text** (not just decomposed elements) so it can identify the system-level use-case before emitting queries — element labels alone lose framing context.
- Receives optional `subproducts` from `SubProductAgent`. When present, the prompt forces the union of generated queries to cover every listed surface.
- Translates jargon ("incremental keystrokes", "build string", "error model") → user-facing vocabulary ("search suggestions", "autocomplete", "recommendations").
- Generates `--queries-per-element` queries per element (default 4).
- Falls back to keyword-only query on LLM failure — never blocks the pipeline.

### Search — `search.py`
- For each (rewritten query, domain) pair runs SerpApi `<query> site:<domain>`.
- Identical (query, domain) pairs are deduped **before** dispatch (`dict.fromkeys`); each unique pair is run exactly once per call.
- Unique queries dispatched in parallel via `ThreadPoolExecutor(max_workers=--search-workers)`. The thread pool is the rate limiter — `sleep_seconds` is retained in the constructor for back-compat but no longer used.
- `_filter_results` accepts a hit only if `utils.domain_matches(hit_domain, target)` is true (exact / subdomain / parent).
- Optional `--exclude-url-patterns` regex blocklist drops obvious non-doc paths.

### Agent 2 — `relevance.py`
- Receives **the full claim text** AND the decomposed elements. The decomposition alone loses context; full claim lets the model make associative jumps ("recommendations" ↔ "presenting most likely items") that the strict per-element rubric otherwise rejects.
- Batches candidate hits (default 35 per batch).
- Batches scored in parallel via `ThreadPoolExecutor(max_workers=--score-workers)`.
- Recall-first prompt: borderline → 0.25, not 0.0.
- **Dedupe** in `_dedupe`: same URL across batches → higher score wins; tied scores merge `matched_elements` and concatenate rationales.

### Post-process: diversity + element coverage (in `finder.py`)
After Agent 2 produces the global ranked list, two generic post-processors run before top-k slicing:

- **Diversity guard** (`_apply_diversity`): within each tied-score tier, URLs sharing a path-prefix bucket (first N segments, default 4) are capped at M (default 3); excess URLs defer to the bottom of the tier. Prevents one feature area from drowning others when many URLs tie at score 1.0. URLs with strictly higher scores are never displaced. Configurable: `--diversity-prefix-segments`, `--diversity-per-prefix`.
- **Element-coverage guarantee** (`_ensure_coverage`): after top-k cut, for each `ClaimElement.id` not represented in top-k, append the highest-scoring candidate (above `--coverage-score-floor`, default 0.5) that matches that element. Output may slightly exceed top-k. Default ON; disable with `--no-element-coverage`.

## Search budget (per run)

```
SerpApi calls ≈ len(DOMAIN_PROBE_QUERIES)                              # Agent 1, currently 5
              + len(domains) * len(elements) * queries_per_element     # search stage
              − duplicate (query, domain) pairs eliminated by cache
```

Default 3 (domains) × ~6 (elements) × 4 (queries) ≈ 72 calls + 5 probes; each then returns up to `--per-domain` (default 10) URLs. Watch quota when raising `--max-domains`, `--queries-per-element`, `--per-domain`, or claim length (more elements).

## Page fetch is load-bearing for precision

SerpApi snippets are short SEO blurbs. Pages whose body actually describes the feature were scored 0.0 from the snippet alone but 0.95 with `--fetch-pages` (e.g. `support.google.com/youtubetv/answer/7271625` — "Recommendations on YouTube TV"). On by default for this reason; disable with `--no-fetch-pages` for cheap/smoke runs. Latency cost is N HTTP requests parallelized over `--fetch-workers`.
