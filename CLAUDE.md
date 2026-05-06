# CLAUDE.md

Project-level guidance for Claude Code. Per-folder CLAUDE.md files own locality-specific detail — read those when editing inside the folder.

## Project Overview

`claim_url` is a Python package that finds official-source URLs evidencing patent-claim limitations for a given product. The previous monolithic `claim_url.py` (~2000 lines) was refactored into a `src/`-layout package with isolated, testable components.

### Pipeline (seven stages + post-process)

1. **Agent 1** (`agents/domain.py::DomainIdentificationAgent`) — discover vendor/official domains via SerpApi probes + LLM classification.
2. **Extractor** (`agents/extractor.py::ClaimElementExtractor`) — decompose claim into 4–8 `ClaimElement`s.
3. **Sub-product probe** (`agents/subproduct.py::SubProductAgent`, default on, disable with `--no-subproduct-probe`) — map the claim onto relevant sub-products / feature surfaces of `{product}`. Generic; no product-specific hardcoding. Output seeds the rewriter and forces per-surface query coverage.
4. **Rewriter** (`agents/rewriter.py::QueryRewriteAgent`) — receives full claim text + sub-products; translates patent jargon → product user-facing vocabulary, distributes queries across surfaces.
5. **Search** (`agents/search.py::OfficialDomainSearch`) — `<query> site:<domain>` per (rewritten query, domain) pair.
6. **Page fetch** (`fetch.py::PageFetcher`, optional `--fetch-pages`) — parallel HTTP fetch + HTML strip → ~4000 chars body to Agent 2.
7. **Agent 2** (`agents/relevance.py::RelevanceCheckingAgent`) — score each URL 0.0–1.0 against the full claim text + decomposed elements.

After scoring, two generic post-processors run before top-k slicing (both default on):
- **Diversity guard** — within tied-score tiers, cap URLs per path-prefix bucket so one feature area can't drown the top-k.
- **Element coverage** — append the highest-scoring candidate (above floor) for any claim element with no representative in top-k.

`finder.py::ClaimURLFinder.run` orchestrates all stages and returns a `FinderResult`.

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
