# CLAUDE.md

Project-level guidance for Claude Code. Per-folder CLAUDE.md files own locality-specific detail — read those when editing inside the folder.

## Project Overview

`claim_url` is a Python package that finds official-source URLs evidencing patent-claim limitations for a given product. The previous monolithic `claim_url.py` (~2000 lines) was refactored into a `src/`-layout package with isolated, testable components.

### Pipeline (8 stages + post-process)

1. **Agent 1** (`agents/domain.py::DomainIdentificationAgent`) — discover vendor/official domains via SerpApi probes + LLM classification. Multi-tenant hosts (github.com, gitlab.com, medium.com, youtube.com, npmjs.com, …) are emitted with the vendor path attached (e.g. `github.com/Netflix`); bare multi-tenant hosts are dropped because `site:github.com` matches every tenant on the platform. Each `DomainCandidate` carries an optional `path_prefix` consumed by search, expansion, and the URL post-filter (`utils.url_matches_spec`).
2. **Extractor** (`agents/extractor.py::ClaimElementExtractor`) — decompose claim into 4–8 `ClaimElement`s. Receives selected paragraphs of patent description as context.
3. **Use-case classifier** (`agents/use_case.py::UseCaseAgent`, default on, `--no-use-case-classification`) — single LLM call labels the claim's technical use-case (e.g. "vehicle dispatch", "on-device autocomplete") and emits 3–6 vocabulary anchor tokens. Shared with the sub-product probe + rewriter so they target the same use-case.
4. **Sub-product probe** (`agents/subproduct.py::SubProductAgent`, default on, `--no-subproduct-probe`) — SerpApi catalogue probes + catalogue page-body fetch (8000 chars per page) + single LLM filter call against claim + use-case anchors. Generic; no product-specific hardcoding. Output seeds rewriter and forces per-surface query coverage. **Two-step variant** (Step A enumerate → Step B filter, two LLM calls) is opt-in via `--subproduct-two-step` — A/B against single-step showed no top-k gain on test patent, default off.
5. **Rewriter** (`agents/rewriter.py::QueryRewriteAgent`) — full claim text + sub-products + use-case anchors. Translates patent jargon → product vocabulary. Constraints: every query MUST contain at least one of (surface name, use-case anchor, product brand); per-surface cap `ceil(total_queries / num_surfaces)`.
6. **Search** (`agents/search.py::OfficialDomainSearch`) — `<query> site:<domain>` per (rewritten query, domain) pair.
7. **Page fetch** (`fetch.py::PageFetcher`, default on `--fetch-pages`) — parallel HTTP fetch + HTML strip → 4000–6000 chars body. Three-tier fallback chain when requests path fails or returns empty: **(a)** Firecrawl scrape (if `FIRECRAWL_API_KEY` env var set) — bypasses bot detection without local browser, honours Firecrawl `max_age` for free 48h cache; **(b)** Playwright Chromium (adaptive: hosts emitting ≥4 consecutive empty bodies with ≥5 observations auto-promoted, default on `--fetch-adaptive-playwright`). Playwright sync API is pinned to a single dedicated worker thread inside the backend (greenlet binding requirement); page-fetch workers submit and block on the result. Raw HTML cached in memory for the harvest stage.
8. **Index-page link harvest** (`agents/expansion.py::IndexLinkHarvester`, default on, `--no-index-link-harvest`) — parse raw HTML of likely index/overview pages → enqueue inline same-domain anchors as additional candidates. Multi-pass (index → sub-index → leaves). Cap `--index-link-harvest-max-total` (default 200). No extra SerpApi cost. Catalogue pages from Stage 4 seeded as additional index candidates. Newly-harvested URLs get a second page-fetch pass.
9. **Agent 2** (`agents/relevance.py::RelevanceCheckingAgent`) — score each URL 0.0–1.0 against the full claim text + decomposed elements. Description is NOT injected here (tried, made results worse — see EXPERIMENTS.md).

After scoring, two generic post-processors run before top-k slicing (both default on):
- **Diversity guard** — within tied-score tiers, cap URLs per path-prefix bucket so one feature area can't drown the top-k.
- **Two-tier element coverage** — Pass 1 at `--coverage-score-floor` (default 0.5) appends the highest-scoring candidate for any uncovered element. Pass 2 at `--coverage-score-floor-secondary` (default 0.25) relaxes the floor for any element still missing.

#### Opt-in mechanisms (default OFF, code retained for A/B)

- **`--subproduct-two-step`** — Splits Stage 4 into enumerate + filter. Theoretical popular-API debias; no measurable top-k gain in run13 vs run10 single-step (both 5/13).
- **`--path-expansion`** + family — `agents/expansion.py::PathNeighborhoodExpander`. Issues follow-up SerpApi queries under hot path prefixes. Overlaps with index-link harvest (free) and leaks into off-topic sub-trees in run13. Code in `agents/expansion.py` for revisit; flag flipped on enables it.

`finder.py::ClaimURLFinder.run` orchestrates all stages and returns a `FinderResult`.

#### Optional UI flow: review domains before search

The Gradio UI exposes a **Discover Domains** button that runs only Stage 1 and renders the result as a checklist. The user can uncheck dead/irrelevant domains before pressing Run Search; the surviving subset is passed to `ClaimURLFinder.run` via the new `preselected_domains: list[DomainCandidate]` parameter, bypassing Stage 1 in the main pipeline. This saves the Stage 1 SerpApi probes + LLM classify call when the user already knows the domain shortlist (or wants to drop one).

Public surface:
- `ClaimURLFinder.discover_domains(product, domain_override) -> list[DomainCandidate]` — Stage 1 standalone.
- `ClaimURLFinder.run(..., preselected_domains=None)` — when set, skips internal `_resolve_domains` and keeps the candidate's rationale/confidence/path_prefix intact in the result.

The CLI is unaffected — `--domains` still maps to the existing `domain_override` codepath, which only carries host strings.

For an end-to-end walkthrough with real example data from `trace/run13/`, read [HOW_IT_WORKS.md](HOW_IT_WORKS.md). For run-by-run experiment history (what's been tried, what worked, what failed), read [trace/EXPERIMENTS.md](trace/EXPERIMENTS.md).

All LLM calls go through `llm.LLMClient` — abstracts OpenAI / Anthropic / Google behind a single `complete(...)` with retry+backoff (jittered exponential). JSON outputs parsed via `utils.parse_json_object` (handles markdown fences, prose-wrapped JSON).

### Where to look

| Folder | What lives there | Read when |
|---|---|---|
| [src/claim_url/](src/claim_url/CLAUDE.md) | top-level modules: cli, config, errors, models, utils, finder, fetch, serp, cache, logging | editing CLI, orchestrator, shared utils, HTTP/SerpApi clients, disk cache |
| [src/claim_url/agents/](src/claim_url/agents/CLAUDE.md) | the six pipeline agents | editing pipeline stages, search budget, prompt design |
| [src/claim_url/llm/](src/claim_url/llm/CLAUDE.md) | LLMClient + per-provider adapters | adding a provider, debugging param-compat issues |
| [tests/](tests/CLAUDE.md) | pytest, mocked LLM + Serp | adding/modifying tests |

## Common commands

Always use the global venv (`/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python`) — see global `~/.claude/CLAUDE.md`.

```bash
PY=/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python

# Install (editable + all LLM SDKs + dev tools)
$PY -m pip install -e ".[all,dev]"

# Or install pinned runtime deps directly
$PY -m pip install -r requirements.txt

# Run via the module entrypoint
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt

# Or via the installed console script
claim-url --product "YouTube TV" --claim-file claim.txt

# Skip --product → LLM suggests candidate products from the claim and prompts you to pick
$PY -m claim_url --claim-file claim.txt
$PY -m claim_url --claim-file claim.txt --suggest-products 5

# Crank parallelism (defaults: domain=5, search=8, score=4, fetch=8)
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
  --search-workers 16 --score-workers 6 --domain-workers 8

# Claude provider, larger top-k
$PY -m claim_url --llm claude --product "Netflix" --claim-file claim.txt --top-k 15

# Gemini, inline claim text
$PY -m claim_url --llm google --product "Spotify" --claim "A computer-implemented system..."

# Skip Agent 1 — force a domain set
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
  --domains "support.google.com,tv.youtube.com"

# High-recall: bump queries per element and results per query past defaults
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
  --queries-per-element 6 --per-domain 15

# Cheap: 1 rewritten query per element, skip page fetching
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
  --queries-per-element 1 --no-fetch-pages

# Defaults already do high-fidelity fetch + exclude per-show landing pages
# (--fetch-pages on, --exclude-url-patterns "/browse/,/watch\?,/community-guide/").
# Override exclude list (or disable) with --exclude-url-patterns ""
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
  --exclude-url-patterns ""

# JSON output, debug logs
$PY -m claim_url --product "X" --claim-file c.txt --output json --log-level DEBUG --log-file /tmp/run.log

# Fetch claim directly from a patent via PCS API (requires PCS_API_KEY, PCS_API_BASE_URL, PCS_API_PORT)
$PY -m claim_url --product "YouTube TV" --patent "US-20120212660-A1"          # claim 1 (default)
$PY -m claim_url --product "YouTube TV" --patent "US-20120212660-A1" --claim-number 3
$PY -m claim_url --patent "US-20120212660-A1"                                  # no --product → LLM suggests

# Disk cache (default ON, dir = ./.claim_url_cache). Skips repeat SerpApi/LLM/page-fetch calls across runs.
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt --cache-dir .claim_url_cache
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt --no-cache  # disable

# Per-stage JSON artifacts (off by default). Dumps 01_domains → 07_final under DIR
# so you can inspect exactly which queries fired and which URLs each (query, domain)
# returned. Useful when a known URL is missing from the final shortlist.
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt --trace-dir trace/run1

# Sub-product probe (default ON) — maps claim onto sub-surfaces of an umbrella
# product (e.g. AWS, Salesforce, Google Maps Platform) and forces the rewriter
# to cover each. Disable for single-coherent-product runs to skip one LLM call.
$PY -m claim_url --product "Google Maps Platform" --claim-file claim.txt --no-subproduct-probe
$PY -m claim_url --product "Google Maps Platform" --claim-file claim.txt --max-subproducts 12

# Top-k post-processors (both default ON) — stop one feature area from drowning
# the top-k, and guarantee per-element coverage in the output.
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
  --diversity-per-prefix 1 --diversity-prefix-segments 5
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
  --no-element-coverage                                 # plain top-k, no append

# Playwright page fetcher (bypasses bot detection, e.g. support.google.com)
# Requires: pip install playwright && playwright install chromium
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt --playwright-fetch

# Adaptive Playwright fallback (default ON) — auto-promotes bot-blocked hosts
# to Playwright after 4 consecutive empty bodies. Disable to keep a pure
# requests-only fetcher (faster smoke runs, weaker recall on bot-blocked hosts).
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt --no-fetch-adaptive-playwright

# Use-case classifier (default ON) — labels the claim's domain in 2-6 words and
# emits 3-6 vocabulary anchors. Shared with sub-product probe + rewriter +
# path expander. Disable to skip one LLM call when the claim is generic enough
# that all downstream stages would derive the same domain anyway.
$PY -m claim_url --product "Google Maps Platform" --claim-file claim.txt --no-use-case-classification

# Path-neighborhood expansion (default OFF — opt-in A/B). Issues follow-up
# SerpApi queries under hot path prefixes. Overlapped with --index-link-harvest
# in run13 and leaked off-topic. Flip on to A/B.
$PY -m claim_url --product "Google Maps Platform" --claim-file claim.txt \
  --path-expansion --path-expansion-max-followups 12

# Sub-product two-step harvest (default OFF — opt-in A/B). Splits Stage 4
# into enumerate + filter (2 LLM calls). No measurable top-k gain vs single-
# step on test patent. Flip on to A/B.
$PY -m claim_url --product "Google Maps Platform" --claim-file claim.txt --subproduct-two-step

# Index-page link harvest (default ON) — parses raw HTML of cached index pages
# and enqueues inline same-domain anchors as additional candidates. No SerpApi
# cost. Tune cap or disable.
$PY -m claim_url --product "Google Maps Platform" --claim-file claim.txt \
  --index-link-harvest-max-total 400
$PY -m claim_url --product "Google Maps Platform" --claim-file claim.txt --no-index-link-harvest

# Two-tier element coverage — primary floor + relaxed secondary floor for niche
# surfaces. Set secondary to 0.0 to disable the fallback pass.
$PY -m claim_url --product "Google Maps Platform" --claim-file claim.txt \
  --coverage-score-floor 0.5 --coverage-score-floor-secondary 0.25

# Eval a trace run against a reference URL set
$PY scripts/eval_runs.py trace/run13 --refs trace/refs_run34.txt

# Run the test suite
$PY -m pytest

# Run with coverage
$PY -m pytest --cov=claim_url --cov-report=term-missing
```

## Required env vars

- `SERPAPI_API_KEY` — **mandatory**.
- `OPENAI_API_KEY` — required when `--llm openai` (default).
- `ANTHROPIC_API_KEY` — required when `--llm claude`.
- `GOOGLE_API_KEY` — required when `--llm google`.
- `PCS_API_KEY` — required when using `--patent` / "Load Claim from Patent" in UI.
- `PCS_API_BASE_URL` — required when using `--patent`.
- `PCS_API_PORT` — optional; used in proxy-mode PCS deployments.
- `FIRECRAWL_API_KEY` — optional. When set, Stage 7's page fetcher uses Firecrawl as a fallback ahead of Playwright whenever the requests path fails or returns an empty body. Bypasses bot detection (e.g. `support.google.com`) without spinning up Chromium. Install SDK with `pip install firecrawl-py` (or `pip install ".[firecrawl]"` / `.[all]`). Logged as `Firecrawl rescued ...` when it fires.

⚠️ **Env var name mismatch:** local `.env` may define `SERP_API_KEY` but the package reads `SERPAPI_API_KEY`. The CLI auto-loads `.env` via `python-dotenv`, but the variable name still has to match. Either rename in `.env` to `SERPAPI_API_KEY` or `export SERPAPI_API_KEY=$SERP_API_KEY` before running.

## Public API surface

```python
from claim_url import (
    ClaimURLFinder, LLMClient, SerpApiClient, PageFetcher, LLMProvider,
    ClaimElement, DomainCandidate, RawHit, ScoredURL, FinderResult,
    ConfigError, __version__,
)
```

## Repository

- Local: `/Users/vinith_macbook_pro/Desktop/python3/EoU` (own `.git`).
- Remote: `git@github.com:VinithVemana/EoU.git` (private).
- `.gitignore` covers `.env`, `__pycache__/`, `*.pyc`, `venv*/`, `*.log`, build artifacts, test/lint caches.

Do **not** stage these files into the parent `uspto-patent-files` repo at `/Users/vinith_macbook_pro/Desktop/python3/` — that was the 2026-04-21 mistake logged in global `CLAUDE.md`.

## Mistakes Log

**2026-05-06 — Called .strip() directly on Gradio textbox values**
DO NOT: call `.strip()` (or any str method) directly on values received from Gradio component callbacks — they are `None` when the textbox is empty, not `""`.
Why: `load_claim_from_patent` did `pcs_api_key.strip()` → `AttributeError: 'NoneType' object has no attribute 'strip'` at runtime even though the textbox existed.
How to apply: Always wrap Gradio textbox inputs with the existing `_text(value)` helper first (`_text(pcs_api_key).strip()`). `_text()` converts `None` → `""` safely. This is the established pattern for every other optional key field in `ui.py` (`llm_api_key`, `serpapi_key`, etc.).

**2026-05-06 — Added unsupported `claim_num` param to PCS parse_claims payload**
DO NOT: add speculative parameters to PCS API payloads without confirming the API supports them.
Why: Adding `"claim_num": 1` to the `parse_claims` payload caused the API to return `{"data": null}`. The original `unwrap()` then returned `None`, and the subsequent `.get()` call crashed with `AttributeError: 'NoneType' object has no attribute 'get'`.
How to apply: Only send payload fields that appear in the working `main()` reference implementation. If an API feature is uncertain, check the response structure first (log/print the raw response) before building logic on top of it. Also fix `unwrap()` defensively: only unwrap `data["data"]` when its value is a non-None dict/list.

**2026-05-07 — Drove Playwright sync API from a multi-worker ThreadPoolExecutor**
DO NOT: call Playwright sync API methods (`page.goto`, `browser.new_context`, etc.) from any thread other than the one that called `sync_playwright().__enter__()`. A `threading.Lock` does not help — the API binds to the greenlet of the init thread, not just to mutual exclusion.
Why: `_PlaywrightBackend.fetch` was invoked from the page-fetch `ThreadPoolExecutor(max_workers=8)` workers. First call to a host (e.g. `support.google.com` after adaptive promotion) initialised Playwright on whichever worker happened to win the lock; subsequent calls from sibling workers crashed with `greenlet.error: Cannot switch to a different thread`.
How to apply: Pin all Playwright work to one OS thread. `_PlaywrightBackend` now owns a `ThreadPoolExecutor(max_workers=1)`; every public method (`_ensure_started`, `fetch`, `close`) submits to it and the caller blocks on `future.result()`. Same rule applies to any future sync-API browser automation library (Puppeteer-py, undetected-chromedriver sync mode, etc.).
