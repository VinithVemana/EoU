# CLAUDE.md

Project-level guidance for Claude Code. Per-folder CLAUDE.md files own locality-specific detail — read those when editing inside the folder.

## Project Overview

`claim_url` is a Python package that finds official-source URLs evidencing patent-claim limitations for a given product. The previous monolithic `claim_url.py` (~2000 lines) was refactored into a `src/`-layout package with isolated, testable components.

### Pipeline (six stages)

1. **Agent 1** (`agents/domain.py::DomainIdentificationAgent`) — discover vendor/official domains via SerpApi probes + LLM classification.
2. **Extractor** (`agents/extractor.py::ClaimElementExtractor`) — decompose claim into 4–8 `ClaimElement`s.
3. **Rewriter** (`agents/rewriter.py::QueryRewriteAgent`) — translate patent jargon → product user-facing vocabulary.
4. **Search** (`agents/search.py::OfficialDomainSearch`) — `<query> site:<domain>` per (rewritten query, domain) pair.
5. **Page fetch** (`fetch.py::PageFetcher`, optional `--fetch-pages`) — parallel HTTP fetch + HTML strip → ~4000 chars body to Agent 2.
6. **Agent 2** (`agents/relevance.py::RelevanceCheckingAgent`) — score each URL 0.0–1.0 against the full claim text + decomposed elements.

`finder.py::ClaimURLFinder.run` orchestrates the six stages and returns a `FinderResult`.

All LLM calls go through `llm.LLMClient` — abstracts OpenAI / Anthropic / Google behind a single `complete(...)` with retry+backoff (jittered exponential). JSON outputs parsed via `utils.parse_json_object` (handles markdown fences, prose-wrapped JSON).

### Where to look

| Folder | What lives there | Read when |
|---|---|---|
| [src/claim_url/](src/claim_url/CLAUDE.md) | top-level modules: cli, config, errors, models, utils, finder, fetch, serp, logging | editing CLI, orchestrator, shared utils, HTTP/SerpApi clients |
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
