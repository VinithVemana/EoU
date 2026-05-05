"""Agent 1: identify a product's official web domains.

Replaces any hardcoded product->domain map. Uses SerpApi probe queries
to gather evidence and asks the LLM to classify which domains are
vendor-owned/official.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from claim_url._progress import progress
from claim_url.config import DOMAIN_PROBE_QUERIES
from claim_url.errors import ClaimURLError
from claim_url.llm import LLMClient
from claim_url.models import DomainCandidate
from claim_url.serp import SerpApiClient
from claim_url.utils import normalize_domain, parse_json_object


LOG = logging.getLogger("claim-url-finder")


SYSTEM_PROMPT = """\
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


PROMPT_TEMPLATE = """\
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


class DomainIdentificationAgent:
    def __init__(
        self,
        llm: LLMClient,
        serp: SerpApiClient,
        *,
        max_domains: int = 8,
        search_results_per_query: int = 8,
    ) -> None:
        self._llm = llm
        self._serp = serp
        self.max_domains = max_domains
        self.search_results_per_query = search_results_per_query

    def discover(self, product: str) -> list[DomainCandidate]:
        evidence = self._collect_evidence(product)
        if not evidence:
            raise ClaimURLError("No SerpApi evidence found for domain identification")

        prompt = PROMPT_TEMPLATE.format(
            product=product,
            evidence_json=json.dumps(evidence, indent=2),
            max_domains=self.max_domains,
        )
        text = self._llm.complete(
            system=SYSTEM_PROMPT,
            prompt=prompt,
            max_tokens=2500,
            temperature=0.0,
            json_mode=True,
        )
        data = parse_json_object(text)
        raw_domains = data.get("domains")
        if not isinstance(raw_domains, list):
            raise ClaimURLError("Domain agent returned invalid 'domains' payload")

        candidates = self._coerce_candidates(raw_domains)
        candidates.sort(key=lambda d: d.confidence, reverse=True)
        candidates = candidates[: self.max_domains]
        if not candidates:
            raise ClaimURLError("Domain agent did not identify any official domains")
        return candidates

    def _collect_evidence(self, product: str) -> list[dict[str, str]]:
        evidence: list[dict[str, str]] = []
        for template in progress(DOMAIN_PROBE_QUERIES, desc="Agent1 domain probes", unit="q"):
            query = template.format(product=product)
            try:
                results = self._serp.search(query, num=self.search_results_per_query)
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
        return evidence

    @staticmethod
    def _coerce_candidates(raw_domains: list[Any]) -> list[DomainCandidate]:
        seen: set[str] = set()
        candidates: list[DomainCandidate] = []

        for item in raw_domains:
            if not isinstance(item, dict):
                continue

            domain = normalize_domain(str(item.get("domain") or ""))
            if not domain or domain in seen:
                continue
            seen.add(domain)

            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
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
        return candidates


__all__ = ["DomainIdentificationAgent"]
