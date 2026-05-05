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
serp.py              # SerpApiClient with bounded retries
fetch.py             # PageFetcher: shared requests.Session + ThreadPoolExecutor.fetch_many()
finder.py            # ClaimURLFinder.run() orchestrates the six stages
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

- `--domains` — bypasses Agent 1 entirely. Comma-separated.
- `--queries-per-element` (default 3) — per-element rewritten query budget. Drives recall.
- `--per-domain` — SerpApi `num` per call.
- `--fetch-pages` / `--fetch-workers` — enable Stage 5 (precision boost; see agents/CLAUDE.md).
- `--exclude-url-patterns` — comma-separated regex blocklist applied in `OfficialDomainSearch._filter_results`.
- `--output {table,json}` and `--log-file PATH` are independent; both can be set.

If you add a new flag, update the docstring at the top of `cli.py` (per global mandatory rule) and the example invocations in the root `CLAUDE.md`.

## Page fetch (`fetch.py`)

- Shared `requests.Session` reused across workers (connection pooling).
- `ThreadPoolExecutor` of `--fetch-workers` (default 8); each URL fetched once and cached in-memory by URL for the run.
- HTML stripped with regex (intentionally not BeautifulSoup — tolerates broken HTML, no extra dep).
- Hands ~4000 chars of body text to Agent 2. Larger windows did not improve scores in past testing and increased token cost.

## SerpApi (`serp.py`)

- `SerpApiClient.search(query, num)` with bounded retries on transient HTTP errors.
- Caller (`OfficialDomainSearch`) is responsible for `site:` scoping and the (query, domain) dedupe cache — the client itself does NOT cache.
