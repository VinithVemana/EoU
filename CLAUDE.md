# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`claim_url` is a Python package that finds official-source URLs evidencing patent-claim limitations for a given product. The previous monolithic `claim_url.py` (~2000 lines) has been refactored into a `src/`-layout package with isolated, testable components.

### Pipeline (six stages)

1. **Agent 1 (`agents/domain.py::DomainIdentificationAgent`)** — uses SerpApi probe queries (`{product} official website`, `... official support`, ...) to gather evidence, then asks the LLM to classify which domains are vendor-owned/official. Replaces any hardcoded product→domain map.
2. **`agents/extractor.py::ClaimElementExtractor`** — LLM decomposes the claim into 4–8 discrete `ClaimElement`s (id, label, keywords). Deterministic extractor, not an autonomous agent.
3. **`agents/rewriter.py::QueryRewriteAgent`** — translates patent jargon ("incremental keystrokes", "build string", "error model") into product user-facing vocabulary ("search suggestions", "autocomplete", "recommendations"). Generates `--queries-per-element` queries per element (default 3). Without this step narrow `site:domain` searches mostly return empty. Falls back to keyword-only query on failure.
4. **`agents/search.py::OfficialDomainSearch`** — for each (rewritten query, domain) pair, runs SerpApi `<query> site:<domain>`. Identical (query, domain) pairs share a single SerpApi call via an in-method cache. Hits filtered to URLs whose normalized domain matches the target (or is a sub/parent of it). Optional `--exclude-url-patterns` regex blocklist drops obvious non-doc paths.
5. **`fetch.py::PageFetcher`** *(optional, `--fetch-pages`)* — fetches each unique candidate URL via a shared `requests.Session` in a `ThreadPoolExecutor` (default 8 workers), strips HTML via regex, hands ~4000 chars of body text to Agent 2. Cached by URL.
6. **Agent 2 (`agents/relevance.py::RelevanceCheckingAgent`)** — receives the full claim text *and* the decomposed elements, batches candidate hits (default 35 per batch) and scores each URL 0.0–1.0. Recall-first prompt; borderline → 0.25 not 0.0. Dedupe across batches keeps highest score; tied scores merge `matched_elements` and concatenate rationales.
7. **`finder.py::ClaimURLFinder.run`** orchestrates the six stages and returns a `FinderResult` dataclass.

All LLM calls go through `llm.LLMClient`, which abstracts OpenAI / Anthropic Claude / Google Gemini behind a single `complete(system, prompt, ..., json_mode)` method with retry+backoff (jittered exponential). JSON outputs are parsed with `utils.parse_json_object` (handles markdown fences and prose-wrapped JSON).

### Layout

```
src/claim_url/
  __init__.py            # public API (LLMClient, ClaimURLFinder, models, ...)
  __main__.py            # python -m claim_url
  cli.py                 # argparse + main()
  config.py              # provider enum, default models, env-var names
  errors.py              # ClaimURLError / ConfigError / LLMError / SearchError
  models.py              # ClaimElement, DomainCandidate, RawHit, ScoredURL, FinderResult
  utils.py               # normalize_domain, parse_json_object, dedupe, chunked, domain_matches
  logging_setup.py       # configure_logging() — called only by CLI, never by lib import
  _progress.py           # tqdm shim (no-op fallback)
  serp.py                # SerpApiClient with bounded retries
  fetch.py               # PageFetcher: Session pool + parallel fetch_many()
  llm/
    __init__.py
    base.py              # LLMClient facade (retries, jitter)
    openai_provider.py
    claude_provider.py
    google_provider.py
  agents/
    __init__.py
    domain.py            # Agent 1
    extractor.py
    rewriter.py
    search.py            # OfficialDomainSearch + SearchSummary
    relevance.py         # Agent 2
  finder.py              # ClaimURLFinder orchestrator
tests/                   # pytest, mocked LLM + Serp
pyproject.toml           # src-layout, console_scripts: claim-url
requirements.txt
requirements-dev.txt
```

## Common commands

Always use the global venv (`/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python`) — see global `CLAUDE.md`.

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

# Claude provider, larger top-k
$PY -m claim_url --llm claude --product "Netflix" --claim-file claim.txt --top-k 15

# Gemini, inline claim text
$PY -m claim_url --llm google --product "Spotify" --claim "A computer-implemented system..."

# Skip Agent 1 — force a domain set
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
  --domains "support.google.com,tv.youtube.com"

# High-recall: more rewritten queries per element + more results per query
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
  --queries-per-element 5 --per-domain 10

# Cheap: only 1 rewritten query per element (still translates patent-ese -> product-ese)
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt --queries-per-element 1

# Highest fidelity: fetch each candidate page body in parallel and exclude per-show landing pages
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
  --fetch-pages --fetch-workers 8 \
  --exclude-url-patterns "/browse/,/watch\?,/community-guide/"

# JSON output, debug logs
$PY -m claim_url --product "X" --claim-file c.txt --output json --log-level DEBUG --log-file /tmp/run.log

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

⚠️ **Env var name mismatch:** local `.env` may define `SERP_API_KEY` but the package reads `SERPAPI_API_KEY`. The CLI auto-loads `.env` via `python-dotenv`, but the variable name still has to match. Either rename in `.env` to `SERPAPI_API_KEY` or `export SERPAPI_API_KEY=$SERP_API_KEY` before running.

## Architecture notes

- **Side-effect-free import.** `import claim_url` does NOT load `.env` and does NOT configure logging. Both happen only inside `cli.main()` so library consumers retain control.
- **Strict domain filtering** in `OfficialDomainSearch._filter_results` (delegating to `utils.domain_matches`): a hit is accepted only if its normalized domain equals the target, is a subdomain of it, or is the parent of it.
- **Search budget**: SerpApi calls per run ≈ `len(DOMAIN_PROBE_QUERIES)` (Agent 1 probes, currently 5) + `len(domains) * len(elements) * queries_per_element` minus duplicate (query, domain) pairs eliminated by the in-method cache. Default 3-query rewrite × 8 elements × 3 domains ≈ 72 calls. Watch quota when raising `--max-domains`, `--queries-per-element`, or claim length.
- **Query rewriting is load-bearing for recall**: without `QueryRewriteAgent`, raw patent vocabulary returns near-zero hits on narrow `site:` searches. The rewrite closes the patent-ese vs product-ese vocabulary gap.
- **Page-fetch is load-bearing for precision**: SerpApi snippets are short SEO blurbs. Pages whose body describes the feature (e.g. `support.google.com/youtubetv/answer/7271625` — "Recommendations on YouTube TV") were scored 0.0 from snippet alone but 0.95 with `--fetch-pages`. Use `--fetch-pages` for production charting; latency cost is N HTTP requests parallelized over `--fetch-workers` (default 8).
- **Agent 2 receives the full claim text**, not just the decomposed elements. The decomposition loses context; the full claim lets the model make associative jumps ("recommendations" ↔ "presenting most likely items") that the strict per-element rubric otherwise rejects.
- **Provider param compatibility**: `OpenAIProvider` picks `max_completion_tokens` for `gpt-5.x` / `o1` / `o3` / `o4` reasoning models and `max_tokens` for legacy chat models, with a one-shot retry if the API rejects the chosen kwarg. `GoogleProvider` uses `response_mime_type="application/json"` for JSON mode. `ClaudeProvider` has no JSON mode here — it relies on prompt instructions + `parse_json_object` fallback.
- **Lazy SDK imports.** Each provider imports its SDK only on construction; installing only one of `openai` / `anthropic` / `google-genai` is sufficient.
- **Dedupe semantics** in `RelevanceCheckingAgent._dedupe`: same URL across multiple batches → higher score wins; tied scores merge `matched_elements` and concatenate rationales.
- **Logger name** is `"claim-url-finder"`. Handlers attached only by `logging_setup.configure_logging()`.

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

(none yet for this project)
