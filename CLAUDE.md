# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-script tool: [claim_url.py](claim_url.py) — finds official-source URLs that evidence patent-claim limitations for a given product. Pipeline:

1. **Agent 1 (`DomainIdentificationAgent`)** — uses SerpApi probe queries (`{product} official website`, `... official support`, ...) to gather evidence, then asks the LLM to classify which domains are vendor-owned/official. Replaces any hardcoded product→domain map.
2. **`ClaimElementExtractor`** — LLM decomposes the claim into 4–8 discrete `ClaimElement`s (id, label, keywords). Not an "agent" — deterministic extraction step.
3. **`QueryRewriteAgent`** — translates patent jargon ("incremental keystrokes", "build string from keystrokes", "error model") into product user-facing vocabulary ("search suggestions", "autocomplete", "recommendations"). Generates `--queries-per-element` queries per element (default 3). Without this step narrow `site:domain` searches mostly return empty — Google has nothing indexed against patent-ese. Falls back to keyword-only query on failure.
4. **`OfficialDomainSearch`** — for each (rewritten query, domain) pair, runs SerpApi `<query> site:<domain>`. Identical (query, domain) pairs share a single SerpApi call via an in-method cache. Hits filtered to URLs whose normalized domain matches the target (or is a sub/parent of it). Optional `--exclude-url-patterns` regex blocklist drops obvious non-doc paths (e.g. per-show landing pages like `tv.youtube.com/browse/<show>`).
5. **`PageFetcher`** *(optional, `--fetch-pages`)* — fetches each unique candidate URL with `requests`, strips HTML via regex (no BeautifulSoup dep), and hands ~4000 chars of body text to Agent 2 alongside the SerpApi snippet. Cached by URL. Mirrors what websearch tools do internally; the snippet alone is often a generic SEO blurb that underrates relevance.
6. **Agent 2 (`RelevanceCheckingAgent`)** — receives the full claim text *and* the decomposed elements, then batches candidate hits (default 35 per batch) and scores each URL 0.0–1.0. Prompt is recall-first: associative semantic matching ("recommendations" ↔ "presenting most likely items"), borderline → 0.25 not 0.0. Dedupe across batches keeps highest score; tied scores merge `matched_elements` and concatenate rationales.
7. **`ClaimURLFinder.run`** orchestrates the six steps and returns a `FinderResult` dataclass.

All LLM calls go through `LLMClient`, which abstracts OpenAI / Anthropic Claude / Google Gemini behind a single `complete(system, prompt, ..., json_mode)` method with retry+backoff. JSON outputs are parsed with `parse_json_object` (handles markdown fences and prose-wrapped JSON).

## Common Commands

Always use the global venv (`/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python`) — see global `CLAUDE.md`.

```bash
# Install deps (no requirements.txt yet)
/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python -m pip install google-search-results openai anthropic google-genai python-dotenv tqdm requests

# Default OpenAI run from a claim file
/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python claim_url.py --product "YouTube TV" --claim-file claim.txt

# Claude provider, larger top-k
/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python claim_url.py --llm claude --product "Netflix" --claim-file claim.txt --top-k 15

# Gemini, inline claim text
/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python claim_url.py --llm google --product "Spotify" --claim "A computer-implemented system..."

# Skip Agent 1 — force a domain set
/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python claim_url.py --product "YouTube TV" --claim-file claim.txt \
  --domains "support.google.com,tv.youtube.com"

# High-recall: more rewritten queries per element + more results per query
/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python claim_url.py --product "YouTube TV" --claim-file claim.txt \
  --queries-per-element 5 --per-domain 10

# Cheap: only 1 rewritten query per element (still translates patent-ese -> product-ese)
/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python claim_url.py --product "YouTube TV" --claim-file claim.txt \
  --queries-per-element 1

# Highest fidelity: fetch each candidate page body and exclude per-show landing pages
/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python claim_url.py --product "YouTube TV" --claim-file claim.txt \
  --fetch-pages --exclude-url-patterns "/browse/,/watch\?,/community-guide/"

# JSON output, debug logs
/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python claim_url.py --product "X" --claim-file c.txt --output json --log-level DEBUG
```

No tests, no linter config, no build step.

## Required env vars

- `SERPAPI_API_KEY` — **mandatory**.
- `OPENAI_API_KEY` — required when `--llm openai` (default).
- `ANTHROPIC_API_KEY` — required when `--llm claude`.
- `GOOGLE_API_KEY` — required when `--llm google`.

⚠️ **Env var name mismatch:** local `.env` defines `SERP_API_KEY` but the script reads `SERPAPI_API_KEY`. The script *does* auto-load `.env` via `python-dotenv` if installed, but the variable still has to match. Either rename in `.env` to `SERPAPI_API_KEY` or `export SERPAPI_API_KEY=$SERP_API_KEY` before running.

## Architecture Notes

- **Strict domain filtering** in `OfficialDomainSearch.search`: a hit is accepted only if its normalized domain equals the target, is a subdomain of it, or is the parent of it. Adding new acceptance rules here changes recall significantly.
- **Search budget**: SerpApi calls per run ≈ `5` (Agent 1 probes) + `len(domains) * len(elements) * queries_per_element` minus duplicate (query, domain) pairs eliminated by the in-method cache. Default 3-query rewrite × 8 elements × 3 domains ≈ 72 calls. Watch quota when raising `--max-domains`, `--queries-per-element`, or claim length.
- **Query rewriting is load-bearing for recall**: without `QueryRewriteAgent`, raw patent vocabulary (e.g. "incremental keystrokes", "build string", "error model") returns near-zero hits on `site:support.google.com` and similar narrow searches. The rewrite step closes the patent-ese vs product-ese vocabulary gap.
- **Page-fetch is load-bearing for precision**: SerpApi snippets are short SEO blurbs that often understate relevance. Pages whose body describes the relevant feature (e.g. `support.google.com/youtubetv/answer/7271625` — "Recommendations on YouTube TV") were scored 0.0 from snippet alone but 0.95 once `--fetch-pages` was on. Use `--fetch-pages` for any production charting run; the latency cost is N HTTP requests where N = unique candidate URLs (cached).
- **Agent 2 receives the full claim text**, not just the decomposed elements. The decomposition loses context; the full claim lets the model make associative jumps ("recommendations" ↔ "presenting most likely items") that the strict per-element rubric otherwise rejects.
- **OpenAI param compatibility**: `_complete_openai` picks `max_completion_tokens` for `gpt-5.x` / `o1` / `o3` / `o4` reasoning models and `max_tokens` for legacy chat models, with a one-shot retry on 400 errors that name the wrong parameter.
- **JSON-mode quirks**: OpenAI uses `response_format={"type":"json_object"}`, Gemini uses `response_mime_type="application/json"`, Claude has no JSON mode here — `_complete_claude` ignores `json_mode` and relies on prompt instructions + `parse_json_object` fallback.
- **Dedupe semantics** in `RelevanceCheckingAgent.score`: when the same URL appears in multiple batches, the higher score wins; tied scores merge `matched_elements` and concatenate rationales.
- **Logger name** is `"claim-url-finder"`. Console format set in `main()` only — importing this module elsewhere yields no handlers by default.

## Repository

- Local: `/Users/vinith_macbook_pro/Desktop/python3/EoU` (own `.git`).
- Remote: `git@github.com:VinithVemana/EoU.git` (private).
- `.gitignore` covers `.env`, `__pycache__/`, `*.pyc`, `venv*/`, `*.log`.

Do **not** stage these files into the parent `uspto-patent-files` repo at `/Users/vinith_macbook_pro/Desktop/python3/` — that was the 2026-04-21 mistake logged in global `CLAUDE.md`.

## Mistakes Log

(none yet for this project)
