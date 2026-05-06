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
from claim_url.agents.subproduct import SubProductAgent
from claim_url.fetch import PageFetcher
from claim_url.llm import LLMClient
from claim_url.models import DomainCandidate, FinderResult
from claim_url.serp import SerpApiClient
from claim_url.spec_context import SpecContext
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
        enable_subproduct_probe: bool = True,
        max_subproducts: int = 8,
        diversity_prefix_segments: int = 4,
        diversity_per_prefix: int = 3,
        ensure_element_coverage: bool = True,
        coverage_score_floor: float = 0.5,
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
        self.subproduct_agent: Optional[SubProductAgent] = (
            SubProductAgent(
                llm=llm, serp=serp, page_fetcher=page_fetcher,
                max_subproducts=max_subproducts,
            )
            if enable_subproduct_probe else None
        )
        self.diversity_prefix_segments = max(1, int(diversity_prefix_segments))
        self.diversity_per_prefix = max(1, int(diversity_per_prefix))
        self.ensure_element_coverage = bool(ensure_element_coverage)
        self.coverage_score_floor = float(coverage_score_floor)

    def run(
        self,
        *,
        claim: str,
        product: str,
        top_k: int = 10,
        domain_override: Optional[list[str]] = None,
        spec_context: Optional[SpecContext] = None,
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

        spec_text = spec_context.formatted() if spec_context else None
        if spec_text:
            LOG.info(
                "Spec context: %d paragraphs (%s selection) from patent=%s claim=%d",
                len(spec_context.relevant_paragraphs),
                spec_context.selection_method,
                spec_context.patent_number,
                spec_context.claim_number,
            )

        LOG.info("Extracting claim elements")
        elements = self.element_extractor.extract(claim, spec_context=spec_text)
        LOG.info("Extracted %d claim elements", len(elements))
        if self._trace is not None:
            self._trace.write("02_elements.json", {
                "claim_chars": len(claim),
                "elements": [asdict(e) for e in elements],
            })

        subproducts = []
        if self.subproduct_agent is not None:
            LOG.info("Probing %s for sub-product / feature surfaces relevant to claim", product)
            subproducts = self.subproduct_agent.discover(
                product=product, claim=claim, domains=domains, spec_context=spec_text
            )
            if self._trace is not None:
                self._trace.write("02b_subproducts.json", {
                    "count": len(subproducts),
                    "subproducts": [asdict(sp) for sp in subproducts],
                })

        LOG.info("Rewriting claim elements into product-vocabulary search queries")
        elements = self.query_rewriter.rewrite(
            product=product,
            claim=claim,
            elements=elements,
            domains=domains,
            subproducts=subproducts or None,
            spec_context=spec_text,
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

        diversified = self._apply_diversity(scored_all)
        scored_urls = diversified[:top_k]
        if self.ensure_element_coverage:
            scored_urls = self._ensure_coverage(
                top_k_urls=scored_urls,
                pool=diversified,
                element_ids=[e.id for e in elements],
            )

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

    def _path_prefix(self, url: str) -> str:
        """Return the first ``diversity_prefix_segments`` path segments of a URL.

        Used to bucket URLs for the diversity guard. URLs whose path prefixes
        are identical likely document the same feature area; capping per
        bucket prevents one feature from drowning others.
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        segments = [s for s in parsed.path.split("/") if s]
        prefix = "/".join(segments[: self.diversity_prefix_segments])
        return f"{parsed.netloc}/{prefix}"

    def _apply_diversity(self, scored: list) -> list:
        """Within tied-score tiers, cap URLs sharing a path prefix.

        URLs with strictly higher scores are never displaced. Within a single
        score tier we round-robin across path prefixes, deferring excess
        URLs from over-represented prefixes to the bottom of the tier.
        """
        if not scored or self.diversity_per_prefix <= 0:
            return list(scored)

        from collections import defaultdict

        # Group consecutive equal-score runs (input is already sorted desc).
        result: list = []
        i = 0
        n = len(scored)
        while i < n:
            j = i
            while j < n and scored[j].score == scored[i].score:
                j += 1
            tier = scored[i:j]
            if len(tier) <= 1:
                result.extend(tier)
                i = j
                continue

            buckets: dict[str, list] = defaultdict(list)
            order: list[str] = []
            for item in tier:
                key = self._path_prefix(item.url)
                if key not in buckets:
                    order.append(key)
                buckets[key].append(item)

            kept: list = []
            deferred: list = []
            for key in order:
                items = buckets[key]
                kept.extend(items[: self.diversity_per_prefix])
                deferred.extend(items[self.diversity_per_prefix:])
            result.extend(kept)
            result.extend(deferred)
            i = j
        return result

    def _ensure_coverage(
        self,
        *,
        top_k_urls: list,
        pool: list,
        element_ids: list[str],
    ) -> list:
        """Append one URL per uncovered element when a strong candidate exists.

        For each element with no representative in ``top_k_urls`` whose
        ``matched_elements`` references it, find the highest-scoring URL in
        ``pool`` (above ``coverage_score_floor``) that does, and append it.
        Output may exceed ``top_k`` slightly to guarantee per-element coverage.
        """
        if not top_k_urls or not element_ids:
            return top_k_urls

        in_top = {u.url for u in top_k_urls}
        covered: set[str] = set()
        for u in top_k_urls:
            covered.update(u.matched_elements)

        appended: list = list(top_k_urls)
        for eid in element_ids:
            if eid in covered:
                continue
            for cand in pool:
                if cand.url in in_top:
                    continue
                if cand.score < self.coverage_score_floor:
                    break  # pool is sorted desc; remaining are weaker
                if eid in cand.matched_elements:
                    appended.append(cand)
                    in_top.add(cand.url)
                    covered.update(cand.matched_elements)
                    LOG.info(
                        "Coverage guard: appended %s (score=%.2f) for element=%s",
                        cand.url, cand.score, eid,
                    )
                    break
        return appended


__all__ = ["ClaimURLFinder"]
