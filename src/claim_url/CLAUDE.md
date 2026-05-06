# src/claim_url/ — package internals

Top-level modules of the `claim_url` package. Pipeline-stage code lives in [agents/](agents/CLAUDE.md); LLM provider adapters in [llm/](llm/CLAUDE.md). Read those when editing inside.

## Module map

```
__init__.py          # public API re-exports (ClaimURLFinder, LLMClient, SerpApiClient,
                     #   PageFetcher, LLMProvider, ClaimElement, DomainCandidate, RawHit,
                     #   ScoredURL, FinderResult, ConfigError, __version__)
__main__.py          # python -m claim_url → cli.main()
cli.py               # argparse + main(); only place that loads .env and configures logging
config.py            # LLMProvider enum, default model names, env-var names
errors.py            # ClaimURLError → ConfigError / LLMError / SearchError
models.py            # dataclasses: ClaimElement, DomainCandidate, RawHit, ScoredURL, FinderResult
utils.py             # normalize_domain, domain_matches, parse_json_object, dedupe, chunked
logging_setup.py     # configure_logging() — called only by CLI
_progress.py         # tqdm shim (no-op fallback when tqdm not installed)
serp.py              # SerpApiClient with bounded retries + optional DiskCache
fetch.py             # PageFetcher: shared requests.Session + ThreadPoolExecutor.fetch_many() + optional DiskCache
finder.py            # ClaimURLFinder.run() orchestrates the six stages
cache.py             # DiskCache: namespaced sha256-keyed JSON cache for SerpApi/LLM/page bodies
trace.py             # TraceWriter: per-stage JSON artifacts when --trace-dir is set
agents/              # see agents/CLAUDE.md
llm/                 # see llm/CLAUDE.md
```

## Architecture invariants

- **Side-effect-free import.** `import claim_url` does NOT load `.env` and does NOT configure logging. Both happen only inside `cli.main()` so library consumers retain control.
- **Logger name** is `"claim-url-finder"`. Handlers attached only by `logging_setup.configure_logging()`. Do not call `basicConfig` from library code.
- **Strict domain filtering** lives in `utils.domain_matches`: a candidate domain is accepted only if its normalized form equals, is a subdomain of, or is the parent of the target.
- **JSON parsing** always goes through `utils.parse_json_object` — it tolerates markdown fences and prose-wrapped JSON returned by some providers.
- **Errors** must be one of `ConfigError` / `LLMError` / `SearchError` (all subclass `ClaimURLError`). Don't raise bare `RuntimeError` from library code.

## CLI surface (`cli.py`)

`parse_args()` is the canonical reference for flags. Notable ones with non-obvious semantics:

- `--product` — **optional**. If omitted, the CLI runs `ProductSuggestionAgent` and prompts the user to pick from a numbered menu (or type a custom name). Errors out when stdin is non-interactive.
- `--suggest-products` (default 7) — cap on the number of products the suggestion agent returns when `--product` is missing.
- `--domains` — bypasses Agent 1 entirely. Comma-separated.
- `--queries-per-element` (default 4) — per-element rewritten query budget. Drives recall.
- `--per-domain` (default 10) — SerpApi `num` per call.
- `--max-domains` (default 3) — cap on official domains Agent 1 may return.
- `--fetch-pages` / `--no-fetch-pages` / `--fetch-workers` — Stage 5 (precision boost; see agents/CLAUDE.md). On by default.
- `--exclude-url-patterns` — default drops `/browse/`, `/watch\?`, `/community-guide/` (per-show landing pages); pass `""` to disable.
- `--domain-workers` / `--search-workers` / `--score-workers` — thread-pool sizes for parallel SerpApi probes / parallel `(query, domain)` searches / parallel Agent 2 batches.
- `--exclude-url-patterns` — comma-separated regex blocklist applied in `OfficialDomainSearch._filter_results`.
- `--cache-dir` / `--no-cache` — disk cache for SerpApi/LLM/page bodies. ON by default with dir `./.claim_url_cache`.
- `--trace-dir DIR` — off by default. When set, `ClaimURLFinder` writes seven numbered JSON artifacts (`01_domains` … `07_final`) under `DIR`. Use to forensics why a URL was missed: per-(query, domain) raw SerpApi results live in `04_search.json`, full pre-top-k score list in `06_scoring.json`.
- `--output {table,json}` and `--log-file PATH` are independent; both can be set.

If you add a new flag, update the docstring at the top of `cli.py` (per global mandatory rule) and the example invocations in the root `CLAUDE.md`.

## Page fetch (`fetch.py`)

- Shared `requests.Session` reused across workers (connection pooling).
- `ThreadPoolExecutor` of `--fetch-workers` (default 8); each URL fetched once and cached in-memory by URL for the run.
- Optional `DiskCache` layer (CLI passes one in by default) — non-empty bodies persist across runs keyed by `(url, max_chars)`.
- HTML stripped with regex (intentionally not BeautifulSoup — tolerates broken HTML, no extra dep).
- Hands ~4000 chars of body text to Agent 2. Larger windows did not improve scores in past testing and increased token cost.

## SerpApi (`serp.py`)

- `SerpApiClient.search(query, num)` with bounded retries on transient HTTP errors.
- Caller (`OfficialDomainSearch`) is responsible for `site:` scoping and the per-run (query, domain) dedupe cache.
- Optional `DiskCache` (CLI wires one by default) keyed by `(engine, gl, hl, q, num)` — extends dedupe across runs and saves SerpApi credits. Empty result sets are cached too (negative caching) so repeated narrow queries skip the network.

## Disk cache (`cache.py`)

- `DiskCache(root, namespace, enabled=True)` — sha256-keyed JSON files at `<root>/<namespace>/<aa>/<full-hash>.json`.
- Three namespaces in use: `serp`, `llm`, `page`.
- LLM cache only stores deterministic completions (`temperature == 0.0`). Hits are recorded on `UsageStats.cache_hits` / `cached_*_tokens` / `cached_cost_usd` so the run summary can report what the cache saved without polluting actual usage counters.
- CLI flags: `--cache-dir DIR` (default `./.claim_url_cache`) and `--no-cache` to disable. Cache dirs are gitignored.
- If you change a prompt template, existing cache entries become stale but stay valid (different inputs = different hash). To force a clean run pass `--no-cache` or wipe the cache dir.
