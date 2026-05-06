"""High-level pipeline: domain discovery -> element extraction -> query rewrite ->
SerpApi search -> optional page fetch -> relevance scoring."""

from __future__ import annotations

import logging
import re
from typing import Optional

from dataclasses import asdict

from claim_url.agents.domain import DomainIdentificationAgent
from claim_url.agents.extractor import ClaimElementExtractor
from claim_url.agents.relevance import RelevanceCheckingAgent
from claim_url.agents.rewriter import QueryRewriteAgent
from claim_url.agents.search import OfficialDomainSearch
from claim_url.fetch import PageFetcher
from claim_url.llm import LLMClient
from claim_url.models import DomainCandidate, FinderResult
from claim_url.serp import SerpApiClient
from claim_url.trace import TraceWriter


LOG = logging.getLogger("claim-url-finder")


class ClaimURLFinder:
    """Orchestrates the six pipeline stages and returns a :class:`FinderResult`."""

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
        domain_workers: int = 5,
        search_workers: int = 8,
        score_workers: int = 4,
        trace_writer: Optional[TraceWriter] = None,
    ) -> None:
        self.domain_agent = DomainIdentificationAgent(
            llm=llm, serp=serp, max_domains=max_domains, max_workers=domain_workers
        )
        self.element_extractor = ClaimElementExtractor(llm=llm)
        self.query_rewriter = QueryRewriteAgent(
            llm=llm, queries_per_element=queries_per_element
        )
        self.searcher = OfficialDomainSearch(
            serp=serp,
            per_domain=per_domain,
            exclude_url_patterns=exclude_url_patterns,
            max_workers=search_workers,
        )
        self.relevance_agent = RelevanceCheckingAgent(
            llm=llm,
            max_candidates_per_batch=max_candidates_per_batch,
            max_workers=score_workers,
        )
        self.page_fetcher = page_fetcher
        self._trace = trace_writer

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
        if not claim or not claim.strip():
            raise ValueError("claim is required")

        domains = self._resolve_domains(product, domain_override)
        domain_names = [d.domain for d in domains]
        LOG.info("Official domains: %s", ", ".join(domain_names))
        if self._trace is not None:
            self._trace.write("01_domains.json", {
                "product": product,
                "override_used": domain_override is not None,
                "domains": [asdict(d) for d in domains],
            })

        LOG.info("Extracting claim elements")
        elements = self.element_extractor.extract(claim)
        LOG.info("Extracted %d claim elements", len(elements))
        if self._trace is not None:
            self._trace.write("02_elements.json", {
                "claim_chars": len(claim),
                "elements": [asdict(e) for e in elements],
            })

        LOG.info("Rewriting claim elements into product-vocabulary search queries")
        elements = self.query_rewriter.rewrite(
            product=product, elements=elements, domains=domains
        )
        rewritten_count = sum(1 for e in elements if e.search_queries)
        LOG.info(
            "Query rewrite: rewritten=%d/%d (rest fall back to keyword query)",
            rewritten_count,
            len(elements),
        )
        if self._trace is not None:
            self._trace.write("03_queries.json", {
                "queries_per_element": self.query_rewriter.queries_per_element,
                "rewritten": rewritten_count,
                "total": len(elements),
                "elements": [
                    {
                        "id": e.id,
                        "label": e.label,
                        "keywords": e.keywords,
                        "search_queries": e.search_queries,
                        "effective_queries": e.queries(product),
                    }
                    for e in elements
                ],
            })

        LOG.info("Searching official domains with SerpApi")
        hits = self.searcher.search(
            product=product, elements=elements, domains=domain_names
        )
        LOG.info("Collected %d raw hits", len(hits))
        if self._trace is not None:
            self._trace.write("04_search.json", {
                "summary": asdict(self.searcher.last_summary),
                "by_query": [
                    {
                        "query": q,
                        "domain": d,
                        "result_count": len(results),
                        "results": [asdict(r) for r in results],
                    }
                    for (q, d), results in self.searcher.last_query_results.items()
                ],
                "kept_hits": [asdict(h) for h in hits],
            })

        if not hits:
            return FinderResult(
                product=product, domains=domains, elements=elements, urls=[]
            )

        if self.page_fetcher is not None:
            bodies = self._enrich_with_bodies(hits)
            if self._trace is not None:
                self._trace.write("05_pagefetch.json", {
                    "urls_requested": len(bodies),
                    "bodies": {url: len(body) for url, body in bodies.items()},
                })

        LOG.info("Scoring relevance")
        scored_all = self.relevance_agent.score(
            product=product, claim=claim, elements=elements, hits=hits
        )
        if self._trace is not None:
            self._trace.write("06_scoring.json", {
                "top_k": top_k,
                "scored_count": len(scored_all),
                "all_scored": [asdict(s) for s in scored_all],
            })
        scored_urls = scored_all[:top_k]

        result = FinderResult(
            product=product, domains=domains, elements=elements, urls=scored_urls
        )
        if self._trace is not None:
            self._trace.write("07_final.json", asdict(result))
        return result

    def _resolve_domains(
        self, product: str, override: Optional[list[str]]
    ) -> list[DomainCandidate]:
        if override:
            return [
                DomainCandidate(
                    domain=d,
                    confidence=1.0,
                    rationale="Provided by --domains override",
                    source_urls=[],
                )
                for d in override
            ]
        LOG.info("Identifying official domains for product=%r", product)
        return self.domain_agent.discover(product)

    def _enrich_with_bodies(self, hits: list[object]) -> dict[str, str]:
        from claim_url.models import RawHit

        assert self.page_fetcher is not None
        unique_urls = sorted({hit.url for hit in hits if isinstance(hit, RawHit)})
        LOG.info("Fetching page bodies for %d unique URLs", len(unique_urls))
        bodies = self.page_fetcher.fetch_many(unique_urls)

        with_body = 0
        for hit in hits:
            if not isinstance(hit, RawHit):
                continue
            body = bodies.get(hit.url, "")
            if body:
                hit.body = body
                with_body += 1

        LOG.info(
            "Page fetch summary: requested=%d hits_with_body=%d",
            len(unique_urls),
            with_body,
        )
        return bodies


__all__ = ["ClaimURLFinder"]
