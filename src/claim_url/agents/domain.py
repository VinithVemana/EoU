"""Agent 1: identify a product's official web domains.

Replaces any hardcoded product->domain map. Uses SerpApi probe queries
to gather evidence and asks the LLM to classify which domains are
vendor-owned/official.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from claim_url._progress import progress
from claim_url.config import DOMAIN_PROBE_QUERIES
from claim_url.errors import ClaimURLError
from claim_url.llm import LLMClient
from claim_url.models import DomainCandidate, SearchResult
from claim_url.serp import SerpApiClient
from claim_url.utils import (
    MULTI_TENANT_HOSTS,
    is_multi_tenant_host,
    normalize_domain,
    parse_domain_spec,
    parse_json_object,
)


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

CRITICAL — multi-tenant hosts:
Some hosts (github.com, gitlab.com, bitbucket.org, medium.com, dev.to,
youtube.com, vimeo.com, twitter.com, x.com, linkedin.com, facebook.com,
instagram.com, npmjs.com, pypi.org, hub.docker.com, readthedocs.io,
substack.com, blogspot.com, wordpress.com, …) are NOT owned by any single
vendor — every URL belongs to a different tenant. For these, the vendor
owns only a SUB-PATH (the org/user/handle), never the bare host.

You MUST express such domains as "host/<vendor-path>" — never as the bare
host. For example:
  "github.com/Netflix"          (Netflix open-source repos)
  "github.com/google"           (Google's GitHub org)
  "youtube.com/@netflix"        (Netflix's YouTube channel)
  "medium.com/netflix-techblog" (publication path)

Returning the bare host (e.g. "github.com") for a multi-tenant host is
WRONG — it would match every repository / channel / publication on the
platform, including unrelated third-party content. If you cannot determine
the vendor's path on a multi-tenant host, omit that host entirely.

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
- single-tenant vendor domains: emit without a path, e.g. "support.google.com"
- multi-tenant hosts (see system prompt list — github.com, gitlab.com,
  medium.com, youtube.com, linkedin.com, npmjs.com, pypi.org, …) MUST be
  emitted with the vendor's path attached, e.g. "github.com/Netflix" or
  "youtube.com/@netflix". Bare multi-tenant hosts will be REJECTED.
"""


class DomainIdentificationAgent:
    def __init__(
        self,
        llm: LLMClient,
        serp: SerpApiClient,
        *,
        max_domains: int = 8,
        search_results_per_query: int = 8,
        max_workers: int = 5,
    ) -> None:
        self._llm = llm
        self._serp = serp
        self.max_domains = max_domains
        self.search_results_per_query = search_results_per_query
        self.max_workers = max(1, int(max_workers))

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
        queries = [t.format(product=product) for t in DOMAIN_PROBE_QUERIES]
        evidence: list[dict[str, str]] = []

        def _probe(query: str) -> tuple[str, list[SearchResult]]:
            try:
                return query, self._serp.search(query, num=self.search_results_per_query)
            except Exception as exc:
                LOG.warning("Domain-discovery search failed query=%r error=%s", query, exc)
                return query, []

        bar = progress(total=len(queries), desc="Agent1 domain probes", unit="q")
        try:
            workers = max(1, min(self.max_workers, len(queries)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_probe, q) for q in queries]
                for future in as_completed(futures):
                    query, results = future.result()
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
                    bar.update(1)
        finally:
            bar.close()
        return evidence

    @staticmethod
    def _coerce_candidates(raw_domains: list[Any]) -> list[DomainCandidate]:
        seen: set[tuple[str, str]] = set()
        candidates: list[DomainCandidate] = []

        for item in raw_domains:
            if not isinstance(item, dict):
                continue

            spec = parse_domain_spec(str(item.get("domain") or ""))
            if spec is None:
                continue
            host, path_prefix = spec.host, spec.path_prefix

            # Reject bare multi-tenant hosts (github.com, medium.com, …) —
            # they would match every tenant on the platform. The LLM is
            # instructed to attach a vendor path; if it didn't, we drop the
            # entry rather than ship a query that returns third-party noise.
            if not path_prefix and is_multi_tenant_host(host):
                LOG.warning(
                    "Dropping multi-tenant host with no path prefix: %r "
                    "(LLM must emit e.g. 'github.com/<org>')",
                    host,
                )
                continue

            key = (host, path_prefix)
            if key in seen:
                continue
            seen.add(key)

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
                    domain=host,
                    confidence=confidence,
                    rationale=str(item.get("rationale") or "").strip(),
                    source_urls=source_urls,
                    path_prefix=path_prefix,
                )
            )
        return candidates


__all__ = ["DomainIdentificationAgent"]
