"""Run ``site:domain`` SerpApi queries for each rewritten element query."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from claim_url._progress import progress
from claim_url.models import ClaimElement, RawHit, SearchResult
from claim_url.serp import SerpApiClient
from claim_url.utils import domain_matches, normalize_domain


LOG = logging.getLogger("claim-url-finder")


@dataclass(slots=True)
class SearchSummary:
    plan_size: int = 0
    unique_queries: int = 0
    api_calls: int = 0
    empty_responses: int = 0
    excluded: int = 0
    hits_kept: int = 0


class OfficialDomainSearch:
    """Search each (rewritten query, domain) pair via SerpApi.

    Identical (query, domain) pairs share a single API call via an
    in-method cache. Hits are filtered to URLs whose normalized domain
    matches the target (or is a sub/parent of it). Optional regex
    blocklist drops obvious non-doc paths before scoring.
    """

    def __init__(
        self,
        serp: SerpApiClient,
        *,
        per_domain: int = 5,
        sleep_seconds: float = 0.2,
        exclude_url_patterns: Optional[list[re.Pattern[str]]] = None,
    ) -> None:
        self._serp = serp
        self.per_domain = per_domain
        self.sleep_seconds = sleep_seconds
        self.exclude_url_patterns = list(exclude_url_patterns or [])
        self.last_summary: SearchSummary = SearchSummary()

    def search(
        self,
        *,
        product: str,  # noqa: ARG002 - retained for API symmetry with element.queries
        elements: Iterable[ClaimElement],
        domains: Iterable[str],
    ) -> list[RawHit]:
        domain_list = list(domains)
        element_list = list(elements)

        plan: list[tuple[ClaimElement, str, str]] = [
            (element, base_query, domain)
            for element in element_list
            for base_query in element.queries(product)
            for domain in domain_list
        ]

        cache: dict[tuple[str, str], list[SearchResult]] = {}
        summary = SearchSummary(plan_size=len(plan))
        bar = progress(total=len(plan), desc="SerpApi search", unit="q")
        hits: list[RawHit] = []

        try:
            for element, base_query, domain in plan:
                full_query = f"{base_query} site:{domain}"
                bar.set_postfix_str(f"{element.id} site:{domain}")

                results = self._run_or_cache(cache, base_query, domain, full_query, summary)
                if not results:
                    summary.empty_responses += 1

                hits.extend(self._filter_results(results, element, domain, summary))
                bar.update(1)
        finally:
            bar.close()

        summary.unique_queries = len(cache)
        self.last_summary = summary

        LOG.info(
            "Search summary: plan=%d unique_queries=%d api_calls=%d empty=%d excluded=%d hits_kept=%d",
            summary.plan_size,
            summary.unique_queries,
            summary.api_calls,
            summary.empty_responses,
            summary.excluded,
            summary.hits_kept,
        )
        return hits

    def _run_or_cache(
        self,
        cache: dict[tuple[str, str], list[SearchResult]],
        base_query: str,
        domain: str,
        full_query: str,
        summary: SearchSummary,
    ) -> list[SearchResult]:
        cache_key = (base_query, domain)
        if cache_key in cache:
            return cache[cache_key]

        try:
            results = self._serp.search(full_query, num=self.per_domain)
            summary.api_calls += 1
        except Exception as exc:
            LOG.warning(
                "Search failed query=%r domain=%s error=%s", base_query, domain, exc
            )
            results = []

        cache[cache_key] = results
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        return results

    def _filter_results(
        self,
        results: list[SearchResult],
        element: ClaimElement,
        domain: str,
        summary: SearchSummary,
    ) -> Iterable[RawHit]:
        for result in results:
            url_domain = normalize_domain(result.url) or ""
            if not domain_matches(url_domain, domain):
                continue

            if self.exclude_url_patterns and any(
                p.search(result.url) for p in self.exclude_url_patterns
            ):
                summary.excluded += 1
                continue

            summary.hits_kept += 1
            yield RawHit(
                url=result.url,
                title=result.title,
                snippet=result.snippet[:1000],
                element_id=element.id,
                domain=domain,
            )


__all__ = ["OfficialDomainSearch", "SearchSummary"]
