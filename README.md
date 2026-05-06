# Claim URL Finder — Patent Claim Charting Pipeline

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776ab?style=for-the-badge" alt="Python">
  <img src="https://img.shields.io/badge/Version-1.0.0-blue?style=for-the-badge" alt="Version">
  <img src="https://img.shields.io/badge/License-Proprietary-critical?style=for-the-badge" alt="License">
  <br>
  <img src="https://img.shields.io/badge/Pipeline-6_Stages-orange?style=for-the-badge" alt="Stages">
  <img src="https://img.shields.io/badge/Concurrency-ThreadPoolExecutor-yellow?style=for-the-badge" alt="Concurrency">
  <img src="https://img.shields.io/badge/Disk_Cache-Enabled-brightgreen?style=for-the-badge" alt="Cache">
  <br>
  <img src="https://img.shields.io/badge/SerpApi-google--search--results-blue?style=for-the-badge" alt="SerpApi">
  <img src="https://img.shields.io/badge/OpenAI-gpt--5.4--mini-412991?style=for-the-badge" alt="OpenAI">
  <img src="https://img.shields.io/badge/Claude-sonnet--4--6-orange?style=for-the-badge" alt="Claude">
  <img src="https://img.shields.io/badge/Gemini-2.5--pro-blueviolet?style=for-the-badge" alt="Gemini">
  <br>
  <img src="https://img.shields.io/badge/requests-2.31%2B-blue?style=for-the-badge" alt="requests">
  <img src="https://img.shields.io/badge/tqdm-4.66%2B-lightgrey?style=for-the-badge" alt="tqdm">
  <img src="https://img.shields.io/badge/python--dotenv-1.0%2B-lightgrey?style=for-the-badge" alt="dotenv">
</p>

<p align="center">
<em>Find official-source URLs that evidence patent-claim limitations for a given product. Six-stage cascade — domain discovery → element extraction → query rewrite → SerpApi search → page fetch → relevance scoring — with disk-backed caching and parallel I/O at every load-bearing stage.</em>
</p>

---

## 📋 Table of Contents

- [Overview](#-overview)
- [The Claim-Charting Pipeline](#-the-claim-charting-pipeline)
- [Pipeline Overview](#-claim-url-finder-pipeline)
- [Step-by-Step Breakdown](#step-by-step-breakdown)
  - [Step 0: Product Suggestion (CLI helper)](#step-0-product-suggestion-cli-helper)
  - [Step 1: Domain Identification (Agent 1)](#step-1-domain-identification-agent-1)
  - [Step 2: Claim Element Extraction](#step-2-claim-element-extraction)
  - [Step 3: Query Rewriting (Patent → Product Vocabulary)](#step-3-query-rewriting-patent--product-vocabulary)
  - [Step 4: Official-Domain Search](#step-4-official-domain-search)
  - [Step 5: Page Fetch (Optional, Default On)](#step-5-page-fetch-optional-default-on)
  - [Step 6: Relevance Scoring (Agent 2)](#step-6-relevance-scoring-agent-2)
- [Appendix A — Pricing Table](#appendix-a--pricing-table)
- [Appendix B — Domain Probe Queries](#appendix-b--domain-probe-queries)
- [Appendix C — Embedded Prompts](#appendix-c--embedded-prompts)

---

## 🎯 Overview

`claim_url` is a Python package that, given a patent claim and a product name, finds the official vendor pages that evidence each technical limitation in the claim. The package was refactored from a 2000-line monolithic script into a `src/`-layout package with isolated, testable components and a public API surface.

The pipeline is **recall-first**: it errs on the side of returning borderline-relevant pages with a 0.25 score so a human reviewer never misses a candidate. Every load-bearing stage runs through a bounded `ThreadPoolExecutor`, and a disk-backed cache (`./.claim_url_cache` by default) makes re-runs free.

### Inputs / Outputs

| Direction | Source | Format | Description |
|:---|:---|:---:|:---|
| **In** | `--claim-file PATH` or `--claim TEXT` | text | Patent claim, one claim per file/string |
| **In** | `--product NAME` *(optional)* | string | Product name; if omitted, LLM suggests + user picks |
| **In** | `--domains a,b,c` *(optional)* | csv | Skip Agent 1 by forcing a domain set |
| **In** | `SERPAPI_API_KEY`, `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | env | API credentials |
| **Out** | `stdout` | text or `--output json` | Ranked URLs + run summary + cache savings |
| **Out** | `./claim_url.log` (configurable via `--log-file`) | text | DEBUG-level log file |
| **Cache** | `./.claim_url_cache/{serp,llm,page}/<sha2>/<hash>.json` | JSON | Read-through cache — disable with `--no-cache` |

### Usage

```bash
PY=/path/to/venv/bin/python

# Default run (LLM=openai, max-domains=3, per-domain=10, queries-per-element=4,
# fetch-pages=on, exclude /browse/,/watch\?,/community-guide/, top-k=10)
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt

# No --product → LLM suggests products; pick interactively
$PY -m claim_url --claim-file claim.txt --suggest-products 5

# Crank parallelism
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
    --search-workers 16 --score-workers 6 --domain-workers 8

# Force domain set (skip Agent 1)
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
    --domains "support.google.com,tv.youtube.com"

# Higher recall — more rewrites, more results per query
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
    --queries-per-element 6 --per-domain 15

# Cheap smoke run
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt \
    --queries-per-element 1 --no-fetch-pages

# Alternate provider
$PY -m claim_url --llm claude --model claude-sonnet-4-6 \
    --product "Netflix" --claim-file claim.txt --top-k 15
$PY -m claim_url --llm google --model gemini-2.5-pro \
    --product "Spotify" --claim "A computer-implemented system..."

# JSON + debug log
$PY -m claim_url --product X --claim-file c.txt --output json \
    --log-level DEBUG --log-file /tmp/run.log

# Cache controls
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt --cache-dir .claim_url_cache
$PY -m claim_url --product "YouTube TV" --claim-file claim.txt --no-cache
```

---

## 📖 The Claim-Charting Pipeline

The package implements a **six-stage cascade** that converts a patent claim and a product name into a scored evidence list. Each stage feeds the next; failure of an *optional* stage degrades quality but never blocks the run.

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  STAGE 1 — Domain Identification         🔍  "Whose docs should I search?"  ║
║                                                                              ║
║  SerpApi probes (5 queries, in parallel) → LLM classifies vendor-owned       ║
║  domains. Replaces any hardcoded product→domain map. Skippable via           ║
║  --domains override.                                                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  STAGE 2 — Claim Element Extraction      📐  "What does the claim require?" ║
║                                                                              ║
║  Single deterministic LLM call decomposes the claim into 4–8 discrete       ║
║  ClaimElement objects, each with id / label / keywords.                      ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  STAGE 3 — Query Rewriting                🤖  "Translate patent-ese → docs" ║
║                                                                              ║
║  Patent jargon ("incremental keystrokes") → product feature names           ║
║  ("autocomplete", "search suggestions"). Generates N queries per element.    ║
║  Load-bearing for recall — without this, narrow site: queries return ∅.     ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  STAGE 4 — Official-Domain Search         🔍  "Hit the docs"                ║
║                                                                              ║
║  For each (rewritten query, domain) pair: SerpApi `<query> site:<domain>`.  ║
║  Identical pairs deduped before dispatch; parallel via ThreadPoolExecutor.   ║
║  Hits filtered by domain match + optional regex blocklist.                   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  STAGE 5 — Page Fetch                     📥  "Read the page body"          ║
║                                                                              ║
║  Default ON. Parallel HTTP fetch via shared requests.Session, regex HTML    ║
║  strip, ~4000 chars handed to Agent 2. Load-bearing for precision —         ║
║  SerpApi snippets are short SEO blurbs and routinely understate relevance.   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  STAGE 6 — Relevance Scoring (Agent 2)    ⚖️  "Score 0.0–1.0 per URL"       ║
║                                                                              ║
║  Receives the FULL claim text + decomposed elements + (optional) page body. ║
║  Batches candidates (35/batch by default), scores in parallel, dedupes      ║
║  across batches keeping the highest score. Recall-first rubric: borderline  ║
║  pages get 0.25, not 0.0.                                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

> **Search budget per run** ≈ `len(DOMAIN_PROBE_QUERIES)` (5) `+ len(domains) × len(elements) × queries_per_element` SerpApi calls, minus identical (query, domain) pairs collapsed by the dedupe layer. Default config ≈ 3 × ~6 × 4 = 72 search calls + 5 probes.

---

## 🔧 Claim URL Finder Pipeline

End-to-end flow traced from `cli.main` → `ClaimURLFinder.run`. Solid arrows = primary path; `❌` = fallback on failure.

```mermaid
flowchart TD
    A([🗂️ --claim-file claim.txt + --product or interactive pick]) --> B

    B["📥 Step 0 — ProductSuggestionAgent.suggest\nLLM nominates 7 candidate products; user picks (only if --product omitted)"]
    B --> C

    C["🔍 Step 1 — DomainIdentificationAgent.discover\n5 SerpApi probes in parallel → LLM classifies vendor-owned domains"]
    C -- ✅ vendor domains found --> D
    C -- ❌ --domains override --> D

    D["📐 Step 2 — ClaimElementExtractor.extract\nSingle deterministic LLM call → 4–8 ClaimElement(id, label, keywords)"]
    D --> E

    E["🤖 Step 3 — QueryRewriteAgent.rewrite\nPatent jargon → product vocabulary; N queries per element"]
    E -- ✅ rewritten queries --> F
    E -- ❌ LLM/parse failure --> F2

    F2["📐 Fallback — ClaimElement.keyword_query\nQuoted product + first 4 keywords (no rewriting)"]
    F2 --> F

    F["🔍 Step 4 — OfficialDomainSearch.search\nDedupe (query, domain) pairs → parallel SerpApi `site:domain` calls\nFilter: domain_matches + --exclude-url-patterns"]
    F -- ✅ raw hits --> G
    F -- ❌ no hits --> Z2

    Z2([🏁 FinderResult(urls=[]) — empty result set])

    G{"📥 Step 5 — PageFetcher.fetch_many\n--fetch-pages on?"}
    G -- ✅ default ON --> H
    G -- ❌ --no-fetch-pages --> I

    H["📥 PageFetcher: shared requests.Session + ThreadPoolExecutor\nRegex HTML strip → first 4000 chars → RawHit.body"]
    H --> I

    I["⚖️ Step 6 — RelevanceCheckingAgent.score\nBatch 35/batch, parallel LLM scoring 0.0–1.0\nDedupe across batches: max score wins, ties merge matched_elements"]
    I --> Z

    Z([🏁 FinderResult — top-k ScoredURL ranked by score, run summary, cache savings])
```

---

## Step-by-Step Breakdown

### Step 0: Product Suggestion (CLI helper)

`ProductSuggestionAgent` is invoked by `cli._resolve_product` only when `--product` is omitted. The LLM is asked to nominate well-known shipping products the claim could plausibly read on; the CLI prints a numbered menu and the user picks an index, types `c` for a custom name, or types a product name directly. Errors out (`ClaimURLError`) when stdin is non-interactive — pass `--product` explicitly in scripts/CI.

- **Input:** raw claim text, `--suggest-products N` (default 7)
- **Output:** `list[ProductSuggestion(name, vendor, rationale)]`
- **Logic:** one LLM call with `temperature=0.0`, `json_mode=True`, `max_tokens=1500`. Soft-fails to an empty list on parse error so the CLI can fall back to free-form input.

> **Example:** *Claim about predictive autocomplete → suggestions might include `Google Search`, `YouTube TV`, `Spotify`, `Netflix`, `Amazon`, each with a one-line rationale. The user picks `[2]` and the rest of the pipeline runs against `YouTube TV`.*

---

### Step 1: Domain Identification (Agent 1)

`DomainIdentificationAgent.discover` replaces any hardcoded product→domain map. It fires the configured probe queries in parallel, collects the URL/title/snippet evidence, and asks the LLM to classify which domains are vendor-owned. Confidence-sorted, capped at `--max-domains` (default 3).

- **Input:** product name, `--max-domains` cap, `--domain-workers` thread pool size (default 5)
- **Output:** `list[DomainCandidate(domain, confidence, rationale, source_urls)]`
- **Logic:** evidence collection via `ThreadPoolExecutor(max_workers=--domain-workers)`; classification via single LLM call with `json_mode=True`, `max_tokens=2500`. Confidence clamped to `[0.0, 1.0]`.

**Probe queries (`config.DOMAIN_PROBE_QUERIES`):**

```python
(
    "{product} official website",
    "{product} official support",
    "{product} documentation official",
    "{product} help center official",
    "{product} official blog newsroom",
)
```

> **Example:** *For `product="YouTube TV"` the agent typically returns `tv.youtube.com` and `support.google.com` with high confidence and a low-confidence `youtube.com` fallback.*

---

### Step 2: Claim Element Extraction

`ClaimElementExtractor.extract` is a deterministic LLM-driven extractor (not an autonomous agent). It decomposes the claim into 4–8 discrete `ClaimElement` objects.

- **Input:** raw claim text
- **Output:** `list[ClaimElement(id, label, keywords)]`
- **Logic:** one LLM call with `json_mode=True`, `max_tokens=2500`. Each element gets a stable id (`E1`, `E2`, …). Empty-keyword fallback splits the label into the first `MAX_FALLBACK_KEYWORDS=6` tokens.

> **Example:** *Claim "incremental keystrokes used to build a string and an error model that predicts intended items" → `E1: capture incremental keystrokes`, `E2: build query string from keystrokes`, `E3: error model predicts intended item`, `E4: present ranked predictions`.*

---

### Step 3: Query Rewriting (Patent → Product Vocabulary)

`QueryRewriteAgent.rewrite` is **load-bearing for recall**. Without it, raw patent vocabulary returns near-zero hits on narrow `site:` searches. The agent translates patent jargon into the product's user-facing vocabulary.

- **Input:** product name, `list[ClaimElement]`, `list[DomainCandidate]`
- **Output:** the same `list[ClaimElement]` with `search_queries` populated (in-place mutation)
- **Logic:** single LLM call generating `--queries-per-element` queries per element (default 4). On any failure, falls back to `ClaimElement.keyword_query` — quoted product + first 4 keywords — so the pipeline always has *something* to search.

**Translation examples baked into the prompt:**

| Patent vocabulary | Product vocabulary |
|:---|:---|
| `incremental keystrokes from input device` | search suggestions / autocomplete / type to search |
| `build a string from keystrokes` | search bar / remote keyboard |
| `error model` / `ambiguous keystrokes` | search corrections / did you mean / voice search |
| `catalog of items in memory` | library / watchlist / channel guide |
| `ordering items on a display` | home screen / recommendations / lineup |

> **Example:** *Element `E3: error model predicts intended item` → rewritten queries `["YouTube TV did you mean", "YouTube TV search corrections", "YouTube TV voice search", "YouTube TV autocomplete"]`.*

---

### Step 4: Official-Domain Search

`OfficialDomainSearch.search` runs `<query> site:<domain>` for each (rewritten query, domain) pair via SerpApi. Identical pairs are deduped **before** dispatch (`dict.fromkeys`); each unique pair is run exactly once per call. Unique queries dispatched in parallel via `ThreadPoolExecutor(max_workers=--search-workers)` (default 8).

- **Input:** product name, `list[ClaimElement]` (with rewritten queries), domain list, `--per-domain` (default 10), `--exclude-url-patterns`
- **Output:** `list[RawHit(url, title, snippet, element_id, domain)]`
- **Logic:** acceptance rule = `utils.domain_matches(hit_domain, target)` — exact / subdomain / parent. Excluded URLs counted but not returned.

**Default URL exclusion list (`cli.py` argparse default):**

```regex
/browse/,/watch\?,/community-guide/
```

**Domain match rule (`utils.domain_matches`):**

```python
url_domain == target
or url_domain.endswith(f".{target}")
or target.endswith(f".{url_domain}")
```

> **Example:** *`search-suggestions site:support.google.com` returns 10 organic hits. `support.google.com/youtubetv/answer/...` is kept (subdomain match); `support.google.com/youtubetv/community-guide/foo` is dropped by the default exclusion list.*

---

### Step 5: Page Fetch (Optional, Default On)

`PageFetcher.fetch_many` is **load-bearing for precision**. SerpApi snippets are short SEO blurbs and routinely understate page relevance; pages whose feature-specific vocabulary lives in the body score `0.0` from snippet alone but `0.95` with `--fetch-pages` (e.g. `support.google.com/youtubetv/answer/7271625` — *"Recommendations on YouTube TV"*).

- **Input:** unique candidate URLs, `--fetch-max-chars` (default 4000), `--fetch-timeout` (default 10s), `--fetch-workers` (default 8)
- **Output:** `dict[url, body_text]` — empty string for any URL that failed to fetch
- **Logic:** shared `requests.Session` (connection pooling); `ThreadPoolExecutor` of `--fetch-workers`; in-memory cache per run + optional `DiskCache` across runs keyed by `(url, max_chars)`. HTML stripped with three regex passes (no BeautifulSoup — tolerates broken HTML, no extra dep).

**HTML strip pipeline (`fetch._strip_html`):**

```regex
<(script|style|noscript)[^>]*>.*?</\1>     # 1. drop script/style/noscript blocks
```

```regex
<[^>]+>                                     # 2. drop remaining tags
```

```regex
\s+                                         # 3. collapse whitespace
```

> **Example:** *Hit `https://support.google.com/youtubetv/answer/7271625` returns ~12KB of HTML; strip + truncate to 4000 chars → "Recommendations on YouTube TV — On the YouTube TV Home tab, you'll see live shows and …" The page body is attached to every `RawHit` for that URL.*

---

### Step 6: Relevance Scoring (Agent 2)

`RelevanceCheckingAgent.score` receives **the full claim text** AND the decomposed elements. The decomposition alone loses context; the full claim lets the model make associative jumps (`recommendations` ↔ `presenting most likely items`) that the strict per-element rubric otherwise rejects.

- **Input:** product name, full claim text, `list[ClaimElement]`, `list[RawHit]` (with optional `body`), `--max-candidates-per-batch` (default 35), `--score-workers` (default 4)
- **Output:** `list[ScoredURL(url, title, snippet, score, matched_elements, rationale)]`, sorted descending, sliced to `--top-k`
- **Logic:** candidates batched and scored in parallel via `ThreadPoolExecutor`. Each batch is one LLM call (`max_tokens=4000`, `json_mode=True`). Scores below `0.0` dropped; scores clamped to `[0.0, 1.0]`. Dedupe across batches: same URL → highest score wins; tied scores merge `matched_elements` and concatenate rationales.

**Scoring rubric (verbatim from prompt):**

| Score | Meaning |
|:---:|:---|
| **1.00** | Body or snippet directly describes product behaviour matching one or more claim limitations |
| **0.75** | Strong evidence — describes the same feature using different vocabulary |
| **0.50** | Adjacent / supporting evidence about a related product feature |
| **0.25** | Weak contextual relevance — page mentions the feature area but does not describe the limitation directly |
| **0.00** | Unrelated to the claim entirely (drop URL) |

**Multi-batch consensus mechanism (`_dedupe`):**

| Same URL appears in… | Result |
|:---|:---|
| 1 batch | Use that score directly |
| Multiple batches, different scores | Highest score wins (single rationale) |
| Multiple batches, **tied** scores | Merge `matched_elements`, concatenate rationales separated by `; ` |

> **Example:** *URL `https://tv.youtube.com/welcome/recommendations/` surfaces in two batches. Batch A scores `0.90` matching `[E3, E4]`; Batch B scores `0.80` matching `[E2, E4]`. Dedupe keeps Batch A's rationale; only `matched_elements=[E3, E4]` survive.*

---

## Appendix A — Pricing Table

`pricing.PRICING` is a longest-prefix-match table of USD-per-1M-token rates used by `UsageStats` to estimate run cost. Some models have a long-context tier triggered when the prompt exceeds `long_context_threshold` (default 200,000 tokens).

> 📎 **Source:** snapshot of provider list prices — update when rates change. Unknown models still get token counts; only the cost line shows `n/a (model not in pricing table)`.

### OpenAI

| Model prefix | Input (short) | Output (short) | Input (long >200k) | Output (long >200k) |
|:---|:---:|:---:|:---:|:---:|
| `gpt-5.5-pro` | 30.00 | 180.00 | 60.00 | 270.00 |
| `gpt-5.5` | 5.00 | 30.00 | 10.00 | 45.00 |
| `gpt-5.4-pro` | 30.00 | 180.00 | 60.00 | 270.00 |
| `gpt-5.4-mini` *(default)* | 0.75 | 4.50 | — | — |
| `gpt-5.4-nano` | 0.20 | 1.25 | — | — |
| `gpt-5.4` | 2.50 | 15.00 | 5.00 | 22.50 |
| `gpt-5.2-pro` | 21.00 | 168.00 | — | — |
| `gpt-5.2` | 1.75 | 14.00 | — | — |
| `gpt-5.1` | 1.25 | 10.00 | — | — |
| `gpt-5-pro` | 15.00 | 120.00 | — | — |
| `gpt-5-mini` | 0.25 | 2.00 | — | — |
| `gpt-5-nano` | 0.05 | 0.40 | — | — |
| `gpt-5` | 1.25 | 10.00 | — | — |
| `gpt-4.1-nano` | 0.10 | 0.40 | — | — |
| `gpt-4.1-mini` | 0.40 | 1.60 | — | — |
| `gpt-4.1` | 2.00 | 8.00 | — | — |
| `gpt-4o-mini` | 0.15 | 0.60 | — | — |
| `gpt-4o` | 2.50 | 10.00 | — | — |
| `gpt-4-turbo` | 10.00 | 30.00 | — | — |
| `gpt-4` | 30.00 | 60.00 | — | — |
| `gpt-3.5-turbo` | 0.50 | 1.50 | — | — |
| `o1-mini` | 3.00 | 12.00 | — | — |
| `o1` | 15.00 | 60.00 | — | — |
| `o3-mini` | 1.10 | 4.40 | — | — |
| `o3` | 2.00 | 8.00 | — | — |
| `o4-mini` | 1.10 | 4.40 | — | — |

### Anthropic

| Model prefix | Input | Output |
|:---|:---:|:---:|
| `claude-opus-4-7` | 5.00 | 25.00 |
| `claude-opus-4-6` | 5.00 | 25.00 |
| `claude-opus-4-5` | 5.00 | 25.00 |
| `claude-opus-4-1` | 15.00 | 75.00 |
| `claude-opus-4` | 15.00 | 75.00 |
| `claude-sonnet-4-6` *(default)* | 3.00 | 15.00 |
| `claude-sonnet-4-5` | 3.00 | 15.00 |
| `claude-sonnet-4` | 3.00 | 15.00 |
| `claude-3-7-sonnet` | 3.00 | 15.00 |
| `claude-haiku-4-5` | 1.00 | 5.00 |
| `claude-3-5-sonnet` | 3.00 | 15.00 |
| `claude-3-5-haiku` | 0.80 | 4.00 |
| `claude-3-opus` | 15.00 | 75.00 |
| `claude-3-sonnet` | 3.00 | 15.00 |
| `claude-3-haiku` | 0.25 | 1.25 |

### Google

| Model prefix | Input (short) | Output (short) | Input (long >200k) | Output (long >200k) |
|:---|:---:|:---:|:---:|:---:|
| `gemini-3.1-pro` | 2.00 | 12.00 | 4.00 | 18.00 |
| `gemini-3.1-flash-lite` | 0.25 | 1.50 | — | — |
| `gemini-3.1-flash-live` | 0.75 | 4.50 | — | — |
| `gemini-3.1-flash` | 0.75 | 4.50 | — | — |
| `gemini-3-pro-image` | 2.00 | 12.00 | — | — |
| `gemini-3-flash` | 0.50 | 3.00 | — | — |
| `gemini-2.5-pro` *(default)* | 1.25 | 10.00 | 2.50 | 15.00 |
| `gemini-2.5-flash-lite` | 0.10 | 0.40 | — | — |
| `gemini-2.5-flash` | 0.30 | 2.50 | — | — |
| `gemini-2.0-flash` | 0.10 | 0.40 | — | — |
| `gemini-1.5-pro` | 1.25 | 5.00 | 2.50 | 10.00 |
| `gemini-1.5-flash` | 0.075 | 0.30 | 0.15 | 0.60 |

---

## Appendix B — Domain Probe Queries

`config.DOMAIN_PROBE_QUERIES` — the five evidence-gathering queries Agent 1 fires per product before classification.

| # | Template |
|:---:|:---|
| 1 | `{product} official website` |
| 2 | `{product} official support` |
| 3 | `{product} documentation official` |
| 4 | `{product} help center official` |
| 5 | `{product} official blog newsroom` |

---

## Appendix C — Embedded Prompts

The prompts below are embedded verbatim in the source and passed to the configured LLM (`openai` / `claude` / `google`). Click any block to expand.

<details>
<summary><strong>📋 Agent 1 — Domain Identification: SYSTEM_PROMPT</strong> &nbsp;·&nbsp; <em>click to expand</em></summary>
<br>

```text
You are an expert web research analyst.

Your task is to identify official web domains for a product.

Only include domains that are likely owned, operated, or officially controlled
by the product vendor or its parent company.

Good examples:
- Product marketing domains
- Official support/help domains
- Official documentation domains
- Official engineering/blog/newsroom domains operated by the vendor
- Parent-company domains if they host official product documentation

Bad examples:
- Wikipedia
- Review sites
- Resellers
- App stores unless the product vendor itself owns the domain
- News articles
- Forums
- Random blogs
- SEO spam
- Social media domains unless the product itself is the social-media site

Return valid JSON only.
```

</details>

---

<details>
<summary><strong>📋 Agent 1 — Domain Identification: PROMPT_TEMPLATE</strong> &nbsp;·&nbsp; <em>click to expand</em></summary>
<br>

```text
Product:
{product}

SerpApi evidence:
{evidence_json}

Identify the official domains that should be searched for product documentation
or official descriptions of product behavior.

Return JSON only using this schema:
{{
  "domains": [
    {{
      "domain": "example.com",
      "confidence": 0.0,
      "rationale": "why this appears official",
      "source_urls": ["https://..."]
    }}
  ]
}}

Rules:
- confidence must be between 0.0 and 1.0
- include at most {max_domains} domains
- prefer high-confidence official domains
- include support/help/documentation subdomains separately if relevant
- normalize domains without paths, for example "support.google.com"
```

</details>

---

<details>
<summary><strong>📋 ClaimElementExtractor: SYSTEM_PROMPT + PROMPT_TEMPLATE</strong> &nbsp;·&nbsp; <em>click to expand</em></summary>
<br>

```text
You are a careful patent analyst. Always return valid JSON.
```

```text
Decompose the following patent claim into 4-8 discrete technical limitations.

For each element output:
- id: short stable id like "E1", "E2", ...
- label: one-sentence plain-English description
- keywords: 3-6 search-friendly keywords or phrases likely to surface product documentation

Rules:
- Do not include legal boilerplate as an element unless it contains a technical limitation.
- Prefer searchable product-behavior phrases.
- Return JSON only.

Schema:
{{
  "elements": [
    {{
      "id": "E1",
      "label": "...",
      "keywords": ["...", "..."]
    }}
  ]
}}

CLAIM:
"""
{claim}
"""
```

</details>

---

<details>
<summary><strong>📋 QueryRewriteAgent: SYSTEM_PROMPT + PROMPT_TEMPLATE</strong> &nbsp;·&nbsp; <em>click to expand</em></summary>
<br>

```text
You translate patent claim limitations into Google search queries that surface official product documentation. Always return valid JSON.
```

```text
Product: {product}

Official domains being searched (with vendor evidence Agent 1 saw):
{domains_json}

Claim elements:
{elements_json}

For each claim element, generate {n} distinct Google search queries that would surface the official product documentation page describing that feature on the listed domains.

Critical translation step:
Patent claims describe behaviour abstractly. Vendor docs use feature names. Translate patent jargon into the product's actual user-facing vocabulary before emitting the query.

Examples of the kind of translation expected:
- "incremental keystrokes from input device" -> "search suggestions" / "autocomplete" / "type to search"
- "build a string from keystrokes" -> "search bar" / "remote keyboard"
- "error model" / "ambiguous keystrokes" -> "search corrections" / "did you mean" / "voice search"
- "catalog of items in memory" -> "library" / "watchlist" / "channel guide"
- "ordering items on a display" -> "home screen" / "recommendations" / "lineup"

Rules:
- 3-7 tokens per query.
- Each element's {n} queries must be distinct: different synonyms, angles, or anchors.
- Do NOT include site: operators. Domain restriction is added by the caller.
- Do NOT wrap the product name in quotes.
- Anchor with the product or its short name when it improves precision; omit when the feature name alone is more natural.
- If the element is generic boilerplate, pick the closest concrete product feature.
- Return JSON only.

Schema:
{{
  "elements": [
    {{
      "id": "E1",
      "queries": ["query 1", "query 2", "query 3"]
    }}
  ]
}}
```

</details>

---

<details>
<summary><strong>📋 Agent 2 — RelevanceCheckingAgent: SYSTEM_PROMPT</strong> &nbsp;·&nbsp; <em>click to expand</em></summary>
<br>

```text
You are a patent claim charting analyst building an evidence list.

Your job is to surface official product documentation that may serve as evidence for any limitation in a patent claim. Recall matters: a human will review the shortlist. Do not be excessively strict — pages that describe the same product behaviour using different vocabulary are valid evidence.

Return valid JSON only.
```

</details>

---

<details>
<summary><strong>📋 Agent 2 — RelevanceCheckingAgent: PROMPT_TEMPLATE</strong> &nbsp;·&nbsp; <em>click to expand</em></summary>
<br>

```text
Product:
{product}

Full patent claim (canonical source of truth — use this for associative semantic matching):
"""
{claim}
"""

Claim elements (decomposed limitations; ids are stable):
{elements_json}

Candidate official URLs:
{candidates_json}

For each candidate URL:
1. Decide which claim element ids the URL evidences. Match against both the decomposed elements AND the full claim. A page that describes the *same product feature* using different terminology than the claim is still a match (e.g. "recommendations" / "search suggestions" / "autocomplete" all map to elements about predicting and presenting likely items even if the claim says "incremental keystrokes" or "error model").
2. Assign a relevance score from 0.0 to 1.0.

Scoring:
- 1.0: Body or snippet directly describes product behaviour matching one or more claim limitations.
- 0.75: Strong evidence — describes the same feature using different vocabulary.
- 0.5: Adjacent/supporting evidence about a related product feature.
- 0.25: Weak contextual relevance — page mentions the feature area but does not describe the limitation directly.
- 0.0: Unrelated to the claim entirely.

Rules:
- When body text is present, weight it more heavily than the snippet — snippets are often generic SEO blurbs that understate relevance.
- Be associative, not literal. The claim uses patent jargon ("incremental keystrokes", "error model", "build string", "alphanumeric symbols"); product docs use feature names ("search", "autocomplete", "recommendations", "voice search", "library", "guide", "lineup"). These are the same thing.
- Only assign 0.0 if the page is genuinely off-topic. Borderline pages should score 0.25, not 0.0.
- Drop URLs that score 0.0 against every element.
- Prefer canonical documentation/help/answer pages over generic landing or per-content pages.
- Return JSON only.

Schema:
{{
  "ranked": [
    {{
      "url": "https://...",
      "score": 0.0,
      "matched_elements": ["E1", "E2"],
      "rationale": "short reason"
    }}
  ]
}}
```

</details>

---

<details>
<summary><strong>📋 ProductSuggestionAgent: SYSTEM_PROMPT + PROMPT_TEMPLATE</strong> &nbsp;·&nbsp; <em>click to expand</em></summary>
<br>

```text
You are a patent licensing analyst. Identify real, currently-shipping commercial products this claim could plausibly read on. Return valid JSON only.
```

```text
Patent claim:
"""
{claim}
"""

Suggest {n} distinct, well-known commercial products this claim could plausibly
describe behavior of. Prefer mainstream products with public official documentation
(help center, support pages, vendor blog).

Rules:
- Each suggestion must be a specific named product, not a category.
- Include the vendor or parent company.
- Give a one-line rationale tying the product to the claim.

Return JSON only:
{{
  "products": [
    {{"name": "Product Name", "vendor": "Vendor", "rationale": "1-line reason"}}
  ]
}}
```

</details>

---
