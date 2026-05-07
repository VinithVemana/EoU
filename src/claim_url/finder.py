"""High-level pipeline: domain discovery -> element extraction -> use-case
classification -> sub-product probe -> query rewrite -> SerpApi search ->
path-neighborhood expansion -> index-page link harvest -> page fetch ->
relevance scoring -> diversity + element coverage.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict
from typing import Optional

from claim_url.agents.domain import DomainIdentificationAgent
from claim_url.agents.expansion import IndexLinkHarvester, PathNeighborhoodExpander
from claim_url.agents.extractor import ClaimElementExtractor
from claim_url.agents.relevance import RelevanceCheckingAgent
from claim_url.agents.rewriter import QueryRewriteAgent
from claim_url.agents.search import OfficialDomainSearch
from claim_url.agents.subproduct import SubProductAgent
from claim_url.agents.use_case import UseCase, UseCaseAgent
from claim_url.fetch import PageFetcher
from claim_url.llm import LLMClient
from claim_url.models import DomainCandidate, FinderResult, RawHit
from claim_url.serp import SerpApiClient
from claim_url.spec_context import SpecContext
from claim_url.trace import TraceWriter
from claim_url.utils import canonicalize_url, parse_domain_spec


LOG = logging.getLogger("claim-url-finder")


class ClaimURLFinder:
    """Orchestrates the full pipeline and returns a :class:`FinderResult`."""

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
        subproduct_two_step_harvest: bool = False,
        enable_use_case_classification: bool = True,
        enable_path_expansion: bool = False,
        path_expansion_max_followups: int = 12,
        path_expansion_min_hits: int = 2,
        path_expansion_prefix_segments: int = 3,
        enable_index_link_harvest: bool = True,
        index_harvest_max_total_links: int = 200,
        diversity_prefix_segments: int = 4,
        diversity_per_prefix: int = 3,
        ensure_element_coverage: bool = True,
        coverage_score_floor: float = 0.5,
        coverage_score_floor_secondary: float = 0.25,
    ) -> None:
        self.domain_agent = DomainIdentificationAgent(
            llm=llm, serp=serp, max_domains=max_domains, max_workers=domain_workers
        )
        self.element_extractor = ClaimElementExtractor(llm=llm)
        self.use_case_agent: Optional[UseCaseAgent] = (
            UseCaseAgent(llm=llm) if enable_use_case_classification else None
        )
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
        self._serp = serp
        self._trace = trace_writer
        self.subproduct_agent: Optional[SubProductAgent] = (
            SubProductAgent(
                llm=llm, serp=serp, page_fetcher=page_fetcher,
                max_subproducts=max_subproducts,
                two_step_harvest=subproduct_two_step_harvest,
            )
            if enable_subproduct_probe else None
        )
        self.enable_path_expansion = bool(enable_path_expansion)
        self.path_expansion_max_followups = max(0, int(path_expansion_max_followups))
        self.path_expansion_min_hits = max(1, int(path_expansion_min_hits))
        self.path_expansion_prefix_segments = max(1, int(path_expansion_prefix_segments))
        self.enable_index_link_harvest = bool(enable_index_link_harvest)
        self.index_harvest_max_total_links = max(1, int(index_harvest_max_total_links))
        self.diversity_prefix_segments = max(1, int(diversity_prefix_segments))
        self.diversity_per_prefix = max(1, int(diversity_per_prefix))
        self.ensure_element_coverage = bool(ensure_element_coverage)
        self.coverage_score_floor = float(coverage_score_floor)
        self.coverage_score_floor_secondary = float(coverage_score_floor_secondary)

    def discover_domains(
        self,
        *,
        product: str,
        domain_override: Optional[list[str]] = None,
    ) -> list[DomainCandidate]:
        """Run only Stage 1 (domain identification) and return the candidates.

        Used by the UI's optional "review domains before search" flow so the
        user can deselect dead/irrelevant domains before paying for the rest
        of the pipeline. Pass the surviving subset back to :meth:`run` via
        ``domain_override`` (or ``preselected_domains`` to keep rationale).
        """
        product = product.strip()
        if not product:
            raise ValueError("product is required")
        return self._resolve_domains(product, domain_override)

    def run(
        self,
        *,
        claim: str,
        product: str,
        top_k: int = 10,
        domain_override: Optional[list[str]] = None,
        preselected_domains: Optional[list[DomainCandidate]] = None,
        spec_context: Optional[SpecContext] = None,
    ) -> FinderResult:
        product = product.strip()
        if not product:
            raise ValueError("product is required")
        if not claim or not claim.strip():
            raise ValueError("claim is required")

        if preselected_domains:
            domains = list(preselected_domains)
        else:
            domains = self._resolve_domains(product, domain_override)
        domain_specs = [d.spec() for d in domains]
        domain_names = [d.domain for d in domains]
        LOG.info(
            "Official domains: %s",
            ", ".join(d.display() for d in domains),
        )
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

        use_case: Optional[UseCase] = None
        if self.use_case_agent is not None:
            use_case = self.use_case_agent.classify(claim=claim, spec_context=spec_text)
            if self._trace is not None:
                self._trace.write("02a_use_case.json", asdict(use_case))

        subproducts = []
        if self.subproduct_agent is not None:
            LOG.info("Probing %s for sub-product / feature surfaces relevant to claim", product)
            subproducts = self.subproduct_agent.discover(
                product=product,
                claim=claim,
                domains=domains,
                spec_context=spec_text,
                use_case=use_case,
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
            use_case=use_case,
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
            product=product, elements=elements, domains=domain_specs
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

        # ------------------------------------------------------------------
        # Path-neighborhood expansion: fan out under hot path prefixes.
        # ------------------------------------------------------------------
        existing_urls: set[str] = {h.url for h in hits}
        already_queried: set[tuple[str, str]] = set(
            self.searcher.last_query_results.keys()
        )
        expansion_hits: list[RawHit] = []
        if self.enable_path_expansion and self.path_expansion_max_followups > 0:
            anchors = list(use_case.anchors) if use_case else []
            expander = PathNeighborhoodExpander(
                serp=self._serp,
                prefix_segments=self.path_expansion_prefix_segments,
                min_hits_per_prefix=self.path_expansion_min_hits,
                max_followups=self.path_expansion_max_followups,
                per_followup=max(5, self.searcher.per_domain),
                anchors=anchors,
            )
            expansion_hits = expander.expand(
                product=product,
                domains=domain_specs,
                existing_hits=hits,
                existing_urls=existing_urls,
                already_queried=already_queried,
            )
            hits.extend(expansion_hits)

        if self._trace is not None:
            self._trace.write("04b_expansion.json", {
                "enabled": self.enable_path_expansion,
                "new_hits": len(expansion_hits),
                "hits": [asdict(h) for h in expansion_hits],
            })

        # ------------------------------------------------------------------
        # Page fetch (pass 1): bodies for all hits gathered so far.
        # ------------------------------------------------------------------
        bodies: dict[str, str] = {}
        if self.page_fetcher is not None:
            bodies = self._enrich_with_bodies(hits)

        # ------------------------------------------------------------------
        # Index-page link harvest: enqueue inline anchors from index pages.
        # Requires the page fetcher to have been invoked above so the raw
        # HTML cache is populated. Catalogue pages already fetched by the
        # sub-product probe are also seeded — they are typically the
        # canonical sub-product index, not always returned by SerpApi.
        # ------------------------------------------------------------------
        index_hits: list[RawHit] = []
        if (
            self.enable_index_link_harvest
            and self.page_fetcher is not None
            and self.index_harvest_max_total_links > 0
        ):
            harvester = IndexLinkHarvester(
                page_fetcher=self.page_fetcher,
                max_total_links=self.index_harvest_max_total_links,
            )
            seed_hits: list[RawHit] = list(hits)
            catalogue_urls: list[str] = []
            if self.subproduct_agent is not None:
                catalogue_urls = list(self.subproduct_agent.last_catalogue_urls)
            for cu in catalogue_urls:
                cu = canonicalize_url(cu)
                if cu in existing_urls:
                    continue
                seed_hits.append(RawHit(
                    url=cu, title="", snippet="",
                    element_id="CATALOGUE",
                    domain=(cu.split("/")[2] if "://" in cu else ""),
                ))
            index_hits = harvester.harvest(
                hits=seed_hits,
                existing_urls=existing_urls,
                domains=domain_specs,
            )
            if index_hits:
                hits.extend(index_hits)
                # Fetch bodies for the newly-harvested links so the relevance
                # agent can score them on real content, not just title.
                self._enrich_with_bodies(index_hits)

        if self._trace is not None:
            self._trace.write("05_pagefetch.json", {
                "urls_requested": len(bodies),
                "bodies": {url: len(body) for url, body in bodies.items()},
                "index_link_hits": len(index_hits),
                "host_stats": (
                    self.page_fetcher.host_stats_snapshot()
                    if self.page_fetcher is not None else {}
                ),
            })

        LOG.info("Scoring relevance")
        scored_all = self.relevance_agent.score(
            product=product, claim=claim, elements=elements, hits=hits,
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
            out: list[DomainCandidate] = []
            for raw in override:
                spec = parse_domain_spec(str(raw))
                if spec is None:
                    LOG.warning("Skipping invalid domain override: %r", raw)
                    continue
                out.append(DomainCandidate(
                    domain=spec.host,
                    confidence=1.0,
                    rationale="Provided by --domains override",
                    source_urls=[],
                    path_prefix=spec.path_prefix,
                ))
            if not out:
                raise ValueError("--domains override produced no valid entries")
            return out
        LOG.info("Identifying official domains for product=%r", product)
        return self.domain_agent.discover(product)

    def _enrich_with_bodies(self, hits: list[RawHit]) -> dict[str, str]:
        if self.page_fetcher is None:
            return {}
        unique_urls = sorted({hit.url for hit in hits if isinstance(hit, RawHit)})
        if not unique_urls:
            return {}
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
        """Two-tier coverage: try the primary floor first; relax to secondary.

        For each element with no representative in ``top_k_urls``, find the
        highest-scoring URL in ``pool`` that matches it. Two passes:

        - Pass 1 uses :attr:`coverage_score_floor` (default 0.5).
        - Pass 2 covers any element still missing using
          :attr:`coverage_score_floor_secondary` (default 0.25).

        Niche / vertical-specific surfaces routinely score in the 0.25–0.50
        band when their pages don't have body text yet (e.g. bot-blocked
        hosts). The secondary pass surfaces them as covering URLs without
        polluting the headline list with weak-tier matches.
        """
        if not top_k_urls or not element_ids:
            return top_k_urls

        in_top = {u.url for u in top_k_urls}
        covered: set[str] = set()
        for u in top_k_urls:
            covered.update(u.matched_elements)

        appended: list = list(top_k_urls)

        def _try_cover(floor: float) -> None:
            for eid in element_ids:
                if eid in covered:
                    continue
                for cand in pool:
                    if cand.url in in_top:
                        continue
                    if cand.score < floor:
                        break  # pool is sorted desc; remaining are weaker
                    if eid in cand.matched_elements:
                        appended.append(cand)
                        in_top.add(cand.url)
                        covered.update(cand.matched_elements)
                        LOG.info(
                            "Coverage guard (floor=%.2f): appended %s "
                            "(score=%.2f) for element=%s",
                            floor, cand.url, cand.score, eid,
                        )
                        break

        _try_cover(self.coverage_score_floor)
        if (
            self.coverage_score_floor_secondary > 0.0
            and self.coverage_score_floor_secondary < self.coverage_score_floor
        ):
            _try_cover(self.coverage_score_floor_secondary)

        return appended


__all__ = ["ClaimURLFinder"]
