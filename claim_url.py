#!/usr/bin/env python3
"""
Claim -> Official-Source URL Finder using SerpApi

Given a patent claim and a product name, this tool:

1. Agent 1 (DomainIdentificationAgent): identifies official domains for the
   product. No hardcoded product-domain map is used.
2. ClaimElementExtractor: decomposes the claim into 4-8 technical elements.
3. QueryRewriteAgent: translates each element from patent jargon ("incremental
   keystrokes", "build string from keystrokes") into the product's user-facing
   vocabulary ("search suggestions", "autocomplete"). Without this step, narrow
   site:domain queries against vendor docs return mostly empty result sets.
4. OfficialDomainSearch: SerpApi calls for each (rewritten query, domain) pair,
   with caching to dedupe identical queries across elements. Optional
   --exclude-url-patterns regex blocklist drops obvious non-doc paths
   (e.g. per-show landing pages).
5. PageFetcher (optional, --fetch-pages): fetches each unique candidate URL,
   strips HTML, hands page body to Agent 2 alongside the SerpApi snippet.
   Mirrors what websearch tools do internally; significantly improves recall
   when snippets are generic SEO blurbs.
6. Agent 2 (RelevanceCheckingAgent): scores/ranks candidate URLs against the
   claim elements using title + snippet + (when fetched) body.

Dependencies:
    pip install google-search-results openai anthropic google-genai python-dotenv tqdm requests

Required env vars:
    SERPAPI_API_KEY     (auto-loaded from .env if python-dotenv is installed)

LLM env vars:
    OPENAI_API_KEY      default provider
    ANTHROPIC_API_KEY   required when --llm claude
    GOOGLE_API_KEY      required when --llm google

Examples:
    # Default OpenAI run, log to ./claim_url.log
    python claim_url.py --product "YouTube TV" --claim-file claim.txt  # default 3 queries/element

    # Claude provider, larger top-k
    python claim_url.py --llm claude --product "Netflix" --claim-file claim.txt --top-k 15

    # Gemini, inline claim
    python claim_url.py --llm google --product "Spotify" --claim "A computer-implemented..."

    # Skip Agent 1 — force domains
    python claim_url.py --product "YouTube TV" --claim-file claim.txt \\
        --domains "support.google.com,tv.youtube.com"

    # Smoke test (smaller search budget)
    python claim_url.py --product "YouTube TV" --claim-file claim.txt \\
        --max-domains 3 --per-domain 3 --top-k 5

    # High-recall run: more rewritten queries per element
    python claim_url.py --product "YouTube TV" --claim-file claim.txt \\
        --queries-per-element 5 --per-domain 10

    # Cheap run: skip query rewriting (queries-per-element=1 still rewrites once)
    python claim_url.py --product "YouTube TV" --claim-file claim.txt \\
        --queries-per-element 1

    # Highest fidelity: fetch each candidate page body and exclude per-show landing pages
    python claim_url.py --product "YouTube TV" --claim-file claim.txt \\
        --fetch-pages --exclude-url-patterns "/browse/,/watch\\?,/channel/"

    # JSON output, custom log file, debug console
    python claim_url.py --product "X" --claim-file c.txt --output json \\
        --log-level DEBUG --log-file /tmp/run.log
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable=None, **kwargs):
        return iterable if iterable is not None else iter([])


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_CLAUDE_MODEL = "claude-3-5-sonnet-latest"
DEFAULT_GOOGLE_MODEL = "gemini-1.5-pro"

LOG = logging.getLogger("claim-url-finder")


class ConfigError(RuntimeError):
    pass


class LLMProvider(str, Enum):
    OPENAI = "openai"
    CLAUDE = "claude"
    GOOGLE = "google"


# =============================================================================
# Data models
# =============================================================================

@dataclass
class ClaimElement:
    """
    A single discrete technical limitation extracted from the patent claim.
    """
    id: str
    label: str
    keywords: list[str]
    search_queries: list[str] = field(default_factory=list)

    def query(self, product: str, max_keywords: int = 4) -> str:
        # Fallback keyword-only query used when QueryRewriteAgent produces nothing.
        # Quotes the product (anchor); keywords unquoted to allow partial matching.
        terms: list[str] = [f'"{product}"']

        for keyword in self.keywords[:max_keywords]:
            keyword = keyword.strip()
            if not keyword:
                continue
            terms.append(keyword)

        return " ".join(terms)

    def queries(self, product: str, max_keywords: int = 4) -> list[str]:
        # Prefer product-vocabulary queries from QueryRewriteAgent.
        # Fall back to raw keyword query if none were generated.
        cleaned = [q.strip() for q in self.search_queries if q and q.strip()]
        if cleaned:
            return cleaned
        return [self.query(product, max_keywords=max_keywords)]


@dataclass
class DomainCandidate:
    domain: str
    confidence: float
    rationale: str = ""
    source_urls: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str


@dataclass
class RawHit:
    url: str
    title: str
    snippet: str
    element_id: str
    domain: str
    body: str = ""


@dataclass
class ScoredURL:
    url: str
    title: str
    snippet: str
    score: float
    matched_elements: list[str] = field(default_factory=list)
    rationale: str = ""


@dataclass
class FinderResult:
    product: str
    domains: list[DomainCandidate]
    elements: list[ClaimElement]
    urls: list[ScoredURL]


# =============================================================================
# Utility functions
# =============================================================================

def strip_markdown_json(text: str) -> str:
    """
    Removes common markdown code fences from LLM JSON output.
    """
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text.strip()).strip()

    return text


def parse_json_object(text: str) -> dict[str, Any]:
    """
    Robustly parse a JSON object from an LLM response.

    LLMs occasionally add prose or markdown despite instructions.
    This attempts strict parse first, then extracts the outermost JSON object.
    """
    cleaned = strip_markdown_json(text)

    try:
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
        return data
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Could not find JSON object in response: {text[:500]}")

    candidate = cleaned[start:end + 1]
    data = json.loads(candidate)

    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")

    return data


def normalize_domain(value: str) -> Optional[str]:
    """
    Normalize a domain or URL into a bare lowercase domain.

    Examples:
        https://support.google.com/youtube -> support.google.com
        www.youtube.com                   -> youtube.com
    """
    if not value:
        return None

    value = value.strip().lower()

    if not value:
        return None

    if "://" not in value:
        value_for_parse = f"https://{value}"
    else:
        value_for_parse = value

    parsed = urlparse(value_for_parse)
    domain = parsed.netloc or parsed.path.split("/")[0]
    domain = domain.strip().lower().strip(".")

    if domain.startswith("www."):
        domain = domain[4:]

    # Remove port if present.
    domain = domain.split(":")[0]

    # Basic domain validation.
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain):
        return None

    return domain


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def dedupe_keep_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)

    return out


# =============================================================================
# LLM abstraction
# =============================================================================

class LLMClient:
    """
    Thin provider abstraction over OpenAI, Anthropic Claude, and Google Gemini.
    """

    def __init__(
        self,
        provider: LLMProvider,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.provider = LLMProvider(provider)

        if self.provider == LLMProvider.OPENAI:
            self.model = model or DEFAULT_OPENAI_MODEL
            api_key = api_key or os.getenv("OPENAI_API_KEY")

            if not api_key:
                raise ConfigError("OPENAI_API_KEY is required for --llm openai")

            from openai import OpenAI

            self.client = OpenAI(api_key=api_key)

        elif self.provider == LLMProvider.CLAUDE:
            self.model = model or DEFAULT_CLAUDE_MODEL
            api_key = api_key or os.getenv("ANTHROPIC_API_KEY")

            if not api_key:
                raise ConfigError("ANTHROPIC_API_KEY is required for --llm claude")

            import anthropic

            self.client = anthropic.Anthropic(api_key=api_key)

        elif self.provider == LLMProvider.GOOGLE:
            self.model = model or DEFAULT_GOOGLE_MODEL
            api_key = api_key or os.getenv("GOOGLE_API_KEY")

            if not api_key:
                raise ConfigError("GOOGLE_API_KEY is required for --llm google")

            from google import genai

            self.client = genai.Client(api_key=api_key)

        else:
            raise ConfigError(f"Unsupported LLM provider: {provider}")

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 3000,
        temperature: float = 0.0,
        json_mode: bool = False,
        retries: int = 3,
    ) -> str:
        last_error: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                if self.provider == LLMProvider.OPENAI:
                    return self._complete_openai(
                        system=system,
                        prompt=prompt,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        json_mode=json_mode,
                    )

                if self.provider == LLMProvider.CLAUDE:
                    return self._complete_claude(
                        system=system,
                        prompt=prompt,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )

                if self.provider == LLMProvider.GOOGLE:
                    return self._complete_google(
                        system=system,
                        prompt=prompt,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        json_mode=json_mode,
                    )

                raise ConfigError(f"Unsupported provider: {self.provider}")

            except Exception as exc:
                last_error = exc
                sleep_seconds = min(2 ** attempt, 10)
                LOG.warning(
                    "LLM call failed attempt=%s/%s provider=%s error=%s",
                    attempt,
                    retries,
                    self.provider.value,
                    exc,
                )
                time.sleep(sleep_seconds)

        raise RuntimeError(f"LLM call failed after {retries} attempts") from last_error

    def _complete_openai(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str:
        # gpt-5.x / o1 / o3 / o4 reasoning models require max_completion_tokens
        # and reject the legacy max_tokens parameter; older chat models use
        # max_tokens. Pick by model-name prefix.
        token_kwarg = (
            "max_completion_tokens"
            if self._uses_max_completion_tokens(self.model)
            else "max_tokens"
        )

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            token_kwarg: max_tokens,
        }

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            # Defensive: some org models flip the requirement unexpectedly.
            # Retry once with the other parameter name if the error names it.
            error_text = str(exc)
            other_kwarg = (
                "max_tokens" if token_kwarg == "max_completion_tokens" else "max_completion_tokens"
            )
            if "max_tokens" in error_text or "max_completion_tokens" in error_text:
                kwargs.pop(token_kwarg, None)
                kwargs[other_kwarg] = max_tokens
                response = self.client.chat.completions.create(**kwargs)
            else:
                raise

        return response.choices[0].message.content or ""

    @staticmethod
    def _uses_max_completion_tokens(model: str) -> bool:
        m = (model or "").lower()
        # OpenAI reasoning + gpt-5 family use max_completion_tokens.
        return (
            m.startswith("gpt-5")
            or m.startswith("o1")
            or m.startswith("o3")
            or m.startswith("o4")
        )

    def _complete_claude(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        response = self.client.messages.create(
            model=self.model,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "user", "content": prompt},
            ],
        )

        parts: list[str] = []

        for block in response.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)

        return "\n".join(parts)

    def _complete_google(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str:
        from google.genai import types

        response_mime_type = "application/json" if json_mode else "text/plain"

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type=response_mime_type,
        )

        response = self.client.models.generate_content(
            model=self.model,
            contents=f"{system}\n\n{prompt}",
            config=config,
        )

        return response.text or ""


# =============================================================================
# SerpApi search client
# =============================================================================

class SerpApiClient:
    """
    Minimal SerpApi Google Search wrapper.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        engine: str = "google",
        gl: str = "us",
        hl: str = "en",
    ) -> None:
        api_key = api_key or os.getenv("SERPAPI_API_KEY")

        if not api_key:
            raise ConfigError("SERPAPI_API_KEY is required")

        self.api_key = api_key
        self.engine = engine
        self.gl = gl
        self.hl = hl

        from serpapi import GoogleSearch

        self._google_search_cls = GoogleSearch

    def search(
        self,
        query: str,
        *,
        num: int = 10,
        retries: int = 3,
        sleep_between_retries: float = 2.0,
    ) -> list[SearchResult]:
        last_error: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                params = {
                    "engine": self.engine,
                    "q": query,
                    "api_key": self.api_key,
                    "num": num,
                    "gl": self.gl,
                    "hl": self.hl,
                }

                LOG.debug("SerpApi query=%s", query)

                search = self._google_search_cls(params)
                data = search.get_dict()

                if "error" in data:
                    error_text = str(data["error"]).lower()
                    # "No results" is a normal outcome for narrow site:domain queries,
                    # not a transient failure. Return empty list so the caller can move on.
                    if "hasn't returned any results" in error_text or "no results" in error_text:
                        LOG.debug("SerpApi no-results query=%r", query)
                        return []
                    raise RuntimeError(data["error"])

                results: list[SearchResult] = []

                for item in data.get("organic_results", []):
                    url = item.get("link") or item.get("url")
                    if not url:
                        continue

                    results.append(
                        SearchResult(
                            url=url,
                            title=item.get("title", "") or "",
                            snippet=item.get("snippet", "") or item.get("content", "") or "",
                        )
                    )

                return results

            except Exception as exc:
                last_error = exc
                LOG.warning(
                    "SerpApi search failed attempt=%s/%s query=%r error=%s",
                    attempt,
                    retries,
                    query,
                    exc,
                )
                time.sleep(sleep_between_retries * attempt)

        raise RuntimeError("SerpApi search failed") from last_error


# =============================================================================
# Page fetcher (optional, used when --fetch-pages is enabled)
# =============================================================================

class PageFetcher:
    """
    Fetches a URL, strips HTML, returns plain text suitable for LLM scoring.

    Why this exists:
        SerpApi snippets are ~150 chars and often generic ("Recommended programs
        on YouTube TV are based on..."). Agent 2 scoring built on snippets alone
        underrates pages whose feature-specific vocabulary lives in the body.
        Websearch tools (OpenAI/Claude) fetch and read pages — this class
        narrows that gap.

    Notes:
        - In-process LRU-ish cache by URL avoids re-fetching duplicates across
          element/domain combinations.
        - Uses regex HTML strip, not BeautifulSoup, to avoid an extra dep.
          Good enough for documentation pages.
        - Failures are swallowed and logged; scoring then falls back to snippet.
    """

    SCRIPT_STYLE_RE = re.compile(
        r"<(script|style|noscript)[^>]*>.*?</\1>",
        re.DOTALL | re.IGNORECASE,
    )
    TAG_RE = re.compile(r"<[^>]+>")
    WS_RE = re.compile(r"\s+")

    def __init__(
        self,
        *,
        max_chars: int = 4000,
        timeout: float = 10.0,
        sleep_seconds: float = 0.2,
        user_agent: Optional[str] = None,
    ) -> None:
        self.max_chars = max_chars
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.user_agent = user_agent or (
            "Mozilla/5.0 (compatible; ClaimURLFinder/1.0; +https://example.invalid/bot)"
        )
        self.cache: dict[str, str] = {}

        try:
            import requests as _requests
        except ImportError as exc:
            raise ConfigError(
                "requests is required for --fetch-pages: pip install requests"
            ) from exc

        self._requests = _requests

    def fetch(self, url: str) -> str:
        if url in self.cache:
            return self.cache[url]

        try:
            response = self._requests.get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                allow_redirects=True,
            )
            response.raise_for_status()
            text = self._strip(response.text)[: self.max_chars]
        except Exception as exc:
            LOG.debug("Page fetch failed url=%s error=%s", url, exc)
            text = ""

        self.cache[url] = text

        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)

        return text

    def _strip(self, html_text: str) -> str:
        import html as html_module

        text = self.SCRIPT_STYLE_RE.sub(" ", html_text)
        text = self.TAG_RE.sub(" ", text)
        text = html_module.unescape(text)
        text = self.WS_RE.sub(" ", text).strip()
        return text


# =============================================================================
# Claim element extraction
# =============================================================================

class ClaimElementExtractor:
    """
    Extracts patent claim elements.
    This is not the relevance agent. It is a deterministic pipeline component
    that uses the selected LLM for structured extraction.
    """

    SYSTEM = "You are a careful patent analyst. Always return valid JSON."

    PROMPT_TEMPLATE = """
Decompose the following patent claim into 4-8 discrete technical limitations.

For each element output:
- id: short stable id like "E1", "E2", ...
- label: one-sentence plain-English description
- keywords: 3-6 search-friendly keywords or phrases likely to surface product documentation

Rules:
- Do not include legal boilerplate as an element unless it contains a technical limitation.
- Prefer searchable product-behavior phrases.
- Return JSON only.
- Schema:
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
\"\"\"
{claim}
\"\"\"
"""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def extract(self, claim: str) -> list[ClaimElement]:
        prompt = self.PROMPT_TEMPLATE.format(claim=claim)

        text = self.llm.complete(
            system=self.SYSTEM,
            prompt=prompt,
            max_tokens=2500,
            temperature=0.0,
            json_mode=True,
        )

        data = parse_json_object(text)
        raw_elements = data.get("elements", [])

        if not isinstance(raw_elements, list) or not raw_elements:
            raise RuntimeError("LLM did not return any claim elements")

        elements: list[ClaimElement] = []

        for idx, item in enumerate(raw_elements, start=1):
            if not isinstance(item, dict):
                continue

            element_id = str(item.get("id") or f"E{idx}").strip()
            label = str(item.get("label") or "").strip()
            keywords = item.get("keywords") or []

            if not label:
                continue

            if not isinstance(keywords, list):
                keywords = []

            clean_keywords = [
                str(k).strip()
                for k in keywords
                if str(k).strip()
            ]

            if not clean_keywords:
                clean_keywords = label.split()[:6]

            elements.append(
                ClaimElement(
                    id=element_id,
                    label=label,
                    keywords=clean_keywords[:8],
                )
            )

        if not elements:
            raise RuntimeError("No valid claim elements extracted")

        return elements


# =============================================================================
# Agent 1: Domain identification
# =============================================================================

class DomainIdentificationAgent:
    """
    Agent 1.

    Identifies official domains for the product dynamically.

    It uses SerpApi to collect evidence, then asks the chosen LLM to classify
    which domains are official product/company/support/documentation domains.

    This replaces the original hardcoded PRODUCT_DOMAINS map.
    """

    SYSTEM = """
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
"""

    PROMPT_TEMPLATE = """
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
"""

    def __init__(
        self,
        llm: LLMClient,
        serp: SerpApiClient,
        *,
        max_domains: int = 8,
        search_results_per_query: int = 8,
    ) -> None:
        self.llm = llm
        self.serp = serp
        self.max_domains = max_domains
        self.search_results_per_query = search_results_per_query

    def discover(self, product: str) -> list[DomainCandidate]:
        queries = [
            f"{product} official website",
            f"{product} official support",
            f"{product} documentation official",
            f"{product} help center official",
            f"{product} official blog newsroom",
        ]

        evidence: list[dict[str, str]] = []

        for query in tqdm(queries, desc="Agent1 domain probes", unit="q"):
            try:
                results = self.serp.search(
                    query,
                    num=self.search_results_per_query,
                )
            except Exception as exc:
                LOG.warning("Domain-discovery search failed query=%r error=%s", query, exc)
                continue

            for result in results:
                evidence.append(
                    {
                        "query": query,
                        "url": result.url,
                        "domain": normalize_domain(result.url) or "",
                        "title": result.title,
                        "snippet": result.snippet[:500],
                    }
                )

        if not evidence:
            raise RuntimeError("No SerpApi evidence found for domain identification")

        prompt = self.PROMPT_TEMPLATE.format(
            product=product,
            evidence_json=json.dumps(evidence, indent=2),
            max_domains=self.max_domains,
        )

        text = self.llm.complete(
            system=self.SYSTEM,
            prompt=prompt,
            max_tokens=2500,
            temperature=0.0,
            json_mode=True,
        )

        data = parse_json_object(text)
        raw_domains = data.get("domains", [])

        if not isinstance(raw_domains, list):
            raise RuntimeError("Domain agent returned invalid domains payload")

        candidates: list[DomainCandidate] = []
        seen: set[str] = set()

        for item in raw_domains:
            if not isinstance(item, dict):
                continue

            domain = normalize_domain(str(item.get("domain") or ""))

            if not domain or domain in seen:
                continue

            seen.add(domain)

            try:
                confidence = float(item.get("confidence", 0.0))
            except Exception:
                confidence = 0.0

            confidence = max(0.0, min(1.0, confidence))

            source_urls = [
                str(url)
                for url in (item.get("source_urls") or [])
                if str(url).strip()
            ][:5]

            candidates.append(
                DomainCandidate(
                    domain=domain,
                    confidence=confidence,
                    rationale=str(item.get("rationale") or "").strip(),
                    source_urls=source_urls,
                )
            )

        candidates.sort(key=lambda d: d.confidence, reverse=True)
        candidates = candidates[: self.max_domains]

        if not candidates:
            raise RuntimeError("Domain agent did not identify any official domains")

        return candidates


# =============================================================================
# Query rewriting (patent-ese -> product-ese)
# =============================================================================

class QueryRewriteAgent:
    """
    Translates patent-style claim limitations into Google search queries that
    use the *product's actual user-facing vocabulary*.

    Why this exists:
        Patent claims describe behaviour abstractly ("incremental keystrokes",
        "build a string", "error model"). Vendor documentation indexes on the
        product's feature names ("search suggestions", "autocomplete", "voice
        search"). Issuing the raw patent terms to Google returns mostly empty
        result sets on narrow site:domain queries.

        This agent uses the LLM (and the evidence Agent 1 already gathered) to
        rewrite each claim element into 2-4 short, product-vocabulary search
        queries before SerpApi calls.
    """

    SYSTEM = """You translate patent claim limitations into Google search queries that surface official product documentation. Always return valid JSON."""

    PROMPT_TEMPLATE = """
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
"""

    def __init__(
        self,
        llm: LLMClient,
        *,
        queries_per_element: int = 3,
    ) -> None:
        self.llm = llm
        self.queries_per_element = queries_per_element

    def rewrite(
        self,
        *,
        product: str,
        elements: list[ClaimElement],
        domains: list[DomainCandidate],
    ) -> list[ClaimElement]:
        if not elements:
            return elements

        domains_payload = [
            {
                "domain": d.domain,
                "rationale": d.rationale,
                "source_urls": d.source_urls[:3],
            }
            for d in domains
        ]

        elements_payload = [
            {"id": e.id, "label": e.label, "keywords": e.keywords}
            for e in elements
        ]

        prompt = self.PROMPT_TEMPLATE.format(
            product=product,
            domains_json=json.dumps(domains_payload, indent=2),
            elements_json=json.dumps(elements_payload, indent=2),
            n=self.queries_per_element,
        )

        try:
            text = self.llm.complete(
                system=self.SYSTEM,
                prompt=prompt,
                max_tokens=2000,
                temperature=0.0,
                json_mode=True,
            )
            data = parse_json_object(text)
        except Exception as exc:
            LOG.warning("Query rewrite failed; falling back to keyword queries error=%s", exc)
            return elements

        raw_elements = data.get("elements", [])
        if not isinstance(raw_elements, list):
            LOG.warning("Query rewrite returned invalid payload; falling back")
            return elements

        by_id: dict[str, list[str]] = {}
        for item in raw_elements:
            if not isinstance(item, dict):
                continue
            element_id = str(item.get("id") or "").strip()
            if not element_id:
                continue
            queries = item.get("queries") or []
            if not isinstance(queries, list):
                continue
            cleaned = [
                str(q).strip()
                for q in queries
                if str(q).strip()
            ]
            cleaned = dedupe_keep_order(cleaned)[: self.queries_per_element]
            if cleaned:
                by_id[element_id] = cleaned

        for element in elements:
            element.search_queries = by_id.get(element.id, [])
            if element.search_queries:
                LOG.debug(
                    "Rewritten queries element=%s queries=%s",
                    element.id,
                    element.search_queries,
                )
            else:
                LOG.warning(
                    "No rewritten queries for element=%s; will use keyword fallback",
                    element.id,
                )

        return elements


# =============================================================================
# Search component
# =============================================================================

class OfficialDomainSearch:
    """
    Searches each claim element against official domains using SerpApi.

    Google site: restrictions are used by querying:
        <element query> site:<domain>
    """

    def __init__(
        self,
        serp: SerpApiClient,
        *,
        per_domain: int = 5,
        sleep_seconds: float = 0.2,
        exclude_url_patterns: Optional[list[re.Pattern[str]]] = None,
    ) -> None:
        self.serp = serp
        self.per_domain = per_domain
        self.sleep_seconds = sleep_seconds
        self.exclude_url_patterns = exclude_url_patterns or []

    def search(
        self,
        *,
        product: str,
        elements: Iterable[ClaimElement],
        domains: Iterable[str],
    ) -> list[RawHit]:
        hits: list[RawHit] = []

        domain_list = list(domains)
        element_list = list(elements)

        # Build (element, base_query, domain) plan up front so the progress bar
        # has a real total. Each element may now contribute multiple queries.
        plan: list[tuple[ClaimElement, str, str]] = []
        for element in element_list:
            for base_query in element.queries(product):
                for domain in domain_list:
                    plan.append((element, base_query, domain))

        # Cache by (base_query, domain) — when two elements yield the same
        # rewritten query, share the SerpApi call.
        cache: dict[tuple[str, str], list[SearchResult]] = {}
        bar = tqdm(total=len(plan), desc="SerpApi search", unit="q")
        empties = 0
        kept = 0
        excluded = 0
        api_calls = 0

        for element, base_query, domain in plan:
            full_query = f"{base_query} site:{domain}"
            bar.set_postfix_str(f"{element.id} site:{domain}")

            cache_key = (base_query, domain)

            if cache_key in cache:
                results = cache[cache_key]
            else:
                try:
                    results = self.serp.search(full_query, num=self.per_domain)
                    api_calls += 1
                except Exception as exc:
                    LOG.warning(
                        "Search failed element=%s query=%r domain=%s error=%s",
                        element.id,
                        base_query,
                        domain,
                        exc,
                    )
                    results = []
                cache[cache_key] = results
                if self.sleep_seconds:
                    time.sleep(self.sleep_seconds)

            if not results:
                empties += 1

            for result in results:
                url_domain = normalize_domain(result.url) or ""

                if not (
                    url_domain == domain
                    or url_domain.endswith(f".{domain}")
                    or domain.endswith(f".{url_domain}")
                ):
                    continue

                if any(p.search(result.url) for p in self.exclude_url_patterns):
                    excluded += 1
                    continue

                hits.append(
                    RawHit(
                        url=result.url,
                        title=result.title,
                        snippet=result.snippet[:1000],
                        element_id=element.id,
                        domain=domain,
                    )
                )
                kept += 1

            bar.update(1)

        bar.close()
        LOG.info(
            "Search summary: plan=%d unique_queries=%d api_calls=%d empty=%d excluded=%d hits_kept=%d",
            len(plan), len(cache), api_calls, empties, excluded, kept,
        )
        return hits


# =============================================================================
# Agent 2: Relevance checking
# =============================================================================

class RelevanceCheckingAgent:
    """
    Agent 2.

    Scores whether each candidate official URL is relevant to the extracted
    claim elements.

    It only uses title/snippet/URL evidence from SerpApi. For higher accuracy,
    you can extend this class to fetch page body text before scoring.
    """

    SYSTEM = """
You are a patent claim charting analyst building an evidence list.

Your job is to surface official product documentation that may serve as evidence for any limitation in a patent claim. Recall matters: a human will review the shortlist. Do not be excessively strict — pages that describe the same product behaviour using different vocabulary are valid evidence.

Return valid JSON only.
"""

    PROMPT_TEMPLATE = """
Product:
{product}

Full patent claim (canonical source of truth — use this for associative semantic matching):
\"\"\"
{claim}
\"\"\"

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
"""

    def __init__(
        self,
        llm: LLMClient,
        *,
        max_candidates_per_batch: int = 35,
    ) -> None:
        self.llm = llm
        self.max_candidates_per_batch = max_candidates_per_batch

    def score(
        self,
        *,
        product: str,
        claim: str,
        elements: list[ClaimElement],
        hits: list[RawHit],
    ) -> list[ScoredURL]:
        if not hits:
            return []

        by_url: dict[str, RawHit] = {}
        surfaced_by: dict[str, set[str]] = {}

        for hit in hits:
            by_url.setdefault(hit.url, hit)
            surfaced_by.setdefault(hit.url, set()).add(hit.element_id)

        candidates = []
        for hit in by_url.values():
            entry = {
                "url": hit.url,
                "title": hit.title,
                "snippet": hit.snippet,
                "domain": hit.domain,
                "surfaced_by_elements": sorted(surfaced_by[hit.url]),
            }
            if hit.body:
                entry["body"] = hit.body
            candidates.append(entry)

        elements_payload = [
            {
                "id": element.id,
                "label": element.label,
                "keywords": element.keywords,
            }
            for element in elements
        ]

        all_scored: list[ScoredURL] = []

        batches = list(chunked(candidates, self.max_candidates_per_batch))
        for batch in tqdm(batches, desc="Agent2 scoring", unit="batch"):
            prompt = self.PROMPT_TEMPLATE.format(
                product=product,
                claim=claim.strip(),
                elements_json=json.dumps(elements_payload, indent=2),
                candidates_json=json.dumps(batch, indent=2),
            )

            try:
                text = self.llm.complete(
                    system=self.SYSTEM,
                    prompt=prompt,
                    max_tokens=4000,
                    temperature=0.0,
                    json_mode=True,
                )
                data = parse_json_object(text)
            except Exception as exc:
                LOG.warning("Relevance batch scoring failed error=%s", exc)
                continue

            ranked = data.get("ranked", [])

            if not isinstance(ranked, list):
                continue

            for item in ranked:
                if not isinstance(item, dict):
                    continue

                url = str(item.get("url") or "").strip()
                hit = by_url.get(url)

                if not hit:
                    continue

                try:
                    score = float(item.get("score", 0.0))
                except Exception:
                    score = 0.0

                score = max(0.0, min(1.0, score))

                if score <= 0.0:
                    continue

                matched_elements = item.get("matched_elements") or []

                if not isinstance(matched_elements, list):
                    matched_elements = []

                clean_matched_elements = [
                    str(e).strip()
                    for e in matched_elements
                    if str(e).strip()
                ]

                all_scored.append(
                    ScoredURL(
                        url=url,
                        title=hit.title,
                        snippet=hit.snippet,
                        score=score,
                        matched_elements=clean_matched_elements,
                        rationale=str(item.get("rationale") or "").strip(),
                    )
                )

        # Deduplicate scored URLs across batches, keeping highest score.
        best_by_url: dict[str, ScoredURL] = {}

        for scored in all_scored:
            existing = best_by_url.get(scored.url)

            if existing is None or scored.score > existing.score:
                best_by_url[scored.url] = scored
            elif existing is not None and scored.score == existing.score:
                existing.matched_elements = dedupe_keep_order(
                    existing.matched_elements + scored.matched_elements
                )
                if scored.rationale and scored.rationale not in existing.rationale:
                    existing.rationale = f"{existing.rationale}; {scored.rationale}".strip("; ")

        output = list(best_by_url.values())
        output.sort(key=lambda x: x.score, reverse=True)

        return output


# =============================================================================
# Orchestration
# =============================================================================

class ClaimURLFinder:
    def __init__(
        self,
        *,
        llm: LLMClient,
        serp: SerpApiClient,
        max_domains: int = 8,
        per_domain: int = 5,
        max_candidates_per_batch: int = 35,
        queries_per_element: int = 3,
        exclude_url_patterns: Optional[list[re.Pattern[str]]] = None,
        page_fetcher: Optional[PageFetcher] = None,
    ) -> None:
        self.domain_agent = DomainIdentificationAgent(
            llm=llm,
            serp=serp,
            max_domains=max_domains,
        )
        self.element_extractor = ClaimElementExtractor(llm=llm)
        self.query_rewriter = QueryRewriteAgent(
            llm=llm,
            queries_per_element=queries_per_element,
        )
        self.searcher = OfficialDomainSearch(
            serp=serp,
            per_domain=per_domain,
            exclude_url_patterns=exclude_url_patterns,
        )
        self.relevance_agent = RelevanceCheckingAgent(
            llm=llm,
            max_candidates_per_batch=max_candidates_per_batch,
        )
        self.page_fetcher = page_fetcher

    def run(
        self,
        *,
        claim: str,
        product: str,
        top_k: int = 10,
        domain_override: Optional[list[str]] = None,
    ) -> FinderResult:
        product = product.strip()

        if not product:
            raise ValueError("product is required")

        if not claim.strip:
            raise ValueError("claim is required")

        LOG.info("Identifying official domains for product=%r", product)

        if domain_override:
            domains = [
                DomainCandidate(
                    domain=d,
                    confidence=1.0,
                    rationale="Provided by --domains override",
                    source_urls=[],
                )
                for d in domain_override
            ]
        else:
            domains = self.domain_agent.discover(product)

        domain_names = [d.domain for d in domains]

        LOG.info("Official domains: %s", ", ".join(domain_names))
        LOG.info("Extracting claim elements")

        elements = self.element_extractor.extract(claim)

        LOG.info("Extracted %s claim elements", len(elements))
        LOG.info("Rewriting claim elements into product-vocabulary search queries")

        elements = self.query_rewriter.rewrite(
            product=product,
            elements=elements,
            domains=domains,
        )

        rewritten_count = sum(1 for e in elements if e.search_queries)
        LOG.info(
            "Query rewrite: rewritten=%d/%d (rest fall back to keyword query)",
            rewritten_count,
            len(elements),
        )

        LOG.info("Searching official domains with SerpApi")

        hits = self.searcher.search(
            product=product,
            elements=elements,
            domains=domain_names,
        )

        LOG.info("Collected %s raw hits", len(hits))

        if not hits:
            return FinderResult(
                product=product,
                domains=domains,
                elements=elements,
                urls=[],
            )

        if self.page_fetcher is not None:
            unique_urls = sorted({hit.url for hit in hits})
            LOG.info("Fetching page bodies for %d unique URLs", len(unique_urls))

            for url in tqdm(unique_urls, desc="Fetching pages", unit="url"):
                self.page_fetcher.fetch(url)

            fetched = 0
            for hit in hits:
                body = self.page_fetcher.cache.get(hit.url, "")
                if body:
                    hit.body = body
                    fetched += 1

            LOG.info(
                "Page fetch summary: requested=%d hits_with_body=%d",
                len(unique_urls),
                fetched,
            )

        LOG.info("Scoring relevance")

        scored_urls = self.relevance_agent.score(
            product=product,
            claim=claim,
            elements=elements,
            hits=hits,
        )

        scored_urls = scored_urls[:top_k]

        return FinderResult(
            product=product,
            domains=domains,
            elements=elements,
            urls=scored_urls,
        )


# =============================================================================
# CLI
# =============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find official product URLs relevant to patent claim elements using SerpApi.",
    )

    parser.add_argument(
        "--product",
        required=True,
        help="Product name, for example 'YouTube TV'.",
    )

    claim_group = parser.add_mutually_exclusive_group(required=True)
    claim_group.add_argument(
        "--claim",
        help="Patent claim text.",
    )
    claim_group.add_argument(
        "--claim-file",
        help="Path to a text file containing the patent claim.",
    )

    parser.add_argument(
        "--llm",
        choices=[provider.value for provider in LLMProvider],
        default=LLMProvider.OPENAI.value,
        help="LLM provider. Default: openai.",
    )

    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Optional model override. Defaults: "
            f"openai={DEFAULT_OPENAI_MODEL}, "
            f"claude={DEFAULT_CLAUDE_MODEL}, "
            f"google={DEFAULT_GOOGLE_MODEL}"
        ),
    )

    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Number of ranked URLs to return. Default: 10.",
    )

    parser.add_argument(
        "--max-domains",
        type=int,
        default=8,
        help="Maximum official domains Agent 1 may return. Default: 8.",
    )

    parser.add_argument(
        "--per-domain",
        type=int,
        default=5,
        help="SerpApi results per claim element per domain. Default: 5.",
    )

    parser.add_argument(
        "--max-candidates-per-batch",
        type=int,
        default=35,
        help="Max URLs per LLM relevance-scoring batch. Default: 35.",
    )

    parser.add_argument(
        "--queries-per-element",
        type=int,
        default=3,
        help=(
            "Number of product-vocabulary search queries the QueryRewriteAgent "
            "generates per claim element. Higher = better recall, more SerpApi "
            "calls. Default: 3."
        ),
    )

    parser.add_argument(
        "--fetch-pages",
        action="store_true",
        help=(
            "Fetch each candidate URL and pass the page body to Agent 2 for "
            "scoring. Mirrors what websearch tools do internally; significantly "
            "improves recall when SerpApi snippets are generic. Adds N HTTP "
            "requests per run (N = unique candidate URLs)."
        ),
    )

    parser.add_argument(
        "--fetch-max-chars",
        type=int,
        default=4000,
        help="Max chars of stripped page text per URL when --fetch-pages is on. Default: 4000.",
    )

    parser.add_argument(
        "--fetch-timeout",
        type=float,
        default=10.0,
        help="HTTP timeout (seconds) per page fetch. Default: 10.",
    )

    parser.add_argument(
        "--exclude-url-patterns",
        default=None,
        help=(
            "Comma-separated regex patterns. Any candidate URL matching one of "
            "these is dropped before scoring. Useful to filter per-content "
            "landing pages (e.g. 'tv\\.youtube\\.com/browse/,/watch\\?'). "
            "Patterns are matched with re.search."
        ),
    )

    parser.add_argument(
        "--domains",
        default=None,
        help=(
            "Optional comma-separated domain override, e.g. "
            "'support.google.com,tv.youtube.com'. "
            "If provided, Agent 1 domain discovery is skipped."
        ),
    )

    parser.add_argument(
        "--serpapi-key",
        default=None,
        help="Optional SerpApi key. Defaults to SERPAPI_API_KEY env var.",
    )

    parser.add_argument(
        "--llm-api-key",
        default=None,
        help=(
            "Optional LLM API key override. Otherwise provider env var is used: "
            "OPENAI_API_KEY, ANTHROPIC_API_KEY, or GOOGLE_API_KEY."
        ),
    )

    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format. Default: text.",
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console logging level. Default: INFO. File log is always DEBUG.",
    )

    parser.add_argument(
        "--log-file",
        default=None,
        help="Path to write the DEBUG-level log file. Default: ./claim_url.log",
    )

    return parser


def read_claim(args: argparse.Namespace) -> str:
    if args.claim_file:
        return Path(args.claim_file).read_text(encoding="utf-8")

    if args.claim:
        return args.claim

    raise ValueError("Either --claim or --claim-file is required")


def parse_url_pattern_list(value: Optional[str]) -> list[re.Pattern[str]]:
    if not value:
        return []

    patterns: list[re.Pattern[str]] = []

    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue

        try:
            patterns.append(re.compile(raw))
        except re.error as exc:
            raise ValueError(f"Invalid --exclude-url-patterns regex {raw!r}: {exc}") from exc

    return patterns


def parse_domain_override(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None

    domains: list[str] = []

    for part in value.split(","):
        domain = normalize_domain(part)
        if domain:
            domains.append(domain)

    domains = dedupe_keep_order(domains)

    if not domains:
        raise ValueError("--domains was provided but no valid domains were found")

    return domains


def print_text_result(result: FinderResult) -> None:
    print("\n=== Product ===")
    print(result.product)

    print("\n=== Official domains identified ===")
    for domain in result.domains:
        print(f"  [{domain.confidence:.2f}] {domain.domain}")
        if domain.rationale:
            print(f"       {domain.rationale}")
        for source_url in domain.source_urls[:3]:
            print(f"       source: {source_url}")

    print("\n=== Claim elements ===")
    for element in result.elements:
        print(f"  {element.id}: {element.label}")
        print(f"       keywords: {', '.join(element.keywords)}")
        if element.search_queries:
            print(f"       queries: {' | '.join(element.search_queries)}")

    print("\n=== Ranked URLs ===")
    if not result.urls:
        print("  No relevant URLs found.")
        return

    for url in result.urls:
        matched = ", ".join(url.matched_elements) or "-"
        print(f"  [{url.score:.2f}] {url.title} ({matched})")
        print(f"       {url.url}")
        if url.rationale:
            print(f"       rationale: {url.rationale}")
        if url.snippet:
            print(f"       snippet: {url.snippet[:300]}")
        print()


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    console_level = getattr(logging, args.log_level.upper())
    fmt = "%(asctime)s %(levelname)s %(name)s - %(message)s"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(fmt))
    root.addHandler(console)

    log_path = Path(args.log_file) if args.log_file else Path("claim_url.log")
    file_h = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(logging.Formatter(fmt))
    root.addHandler(file_h)

    LOG.info("Logging to file: %s", log_path.resolve())

    run_started = time.time()

    try:
        claim = read_claim(args)
        domain_override = parse_domain_override(args.domains)

        llm = LLMClient(
            provider=LLMProvider(args.llm),
            model=args.model,
            api_key=args.llm_api_key,
        )

        serp = SerpApiClient(
            api_key=args.serpapi_key,
        )

        exclude_patterns = parse_url_pattern_list(args.exclude_url_patterns)

        page_fetcher: Optional[PageFetcher] = None
        if args.fetch_pages:
            page_fetcher = PageFetcher(
                max_chars=args.fetch_max_chars,
                timeout=args.fetch_timeout,
            )

        finder = ClaimURLFinder(
            llm=llm,
            serp=serp,
            max_domains=args.max_domains,
            per_domain=args.per_domain,
            max_candidates_per_batch=args.max_candidates_per_batch,
            queries_per_element=args.queries_per_element,
            exclude_url_patterns=exclude_patterns,
            page_fetcher=page_fetcher,
        )

        result = finder.run(
            claim=claim,
            product=args.product,
            top_k=args.top_k,
            domain_override=domain_override,
        )

        if args.output == "json":
            print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
        else:
            print_text_result(result)

        elapsed = time.time() - run_started
        LOG.info(
            "Run summary: domains=%d elements=%d urls=%d elapsed=%.1fs",
            len(result.domains),
            len(result.elements),
            len(result.urls),
            elapsed,
        )

        return 0

    except KeyboardInterrupt:
        LOG.error("Interrupted")
        return 130

    except Exception as exc:
        LOG.exception("Failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())


