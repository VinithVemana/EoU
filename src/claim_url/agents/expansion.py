"""Recall expansion: harvest extra candidate URLs after the initial search.

Two complementary, generic strategies run after :class:`OfficialDomainSearch`
and before scoring. Both add zero patent-specific knowledge.

Path-neighborhood expansion
---------------------------
The initial SerpApi search lands hits clustered by path prefix
(``/maps/documentation/mobility/...``, ``/foundation/uikit/views/...``, …).
Niche surfaces routinely produce one or two hits but never enough for the
full sub-tree to surface. :class:`PathNeighborhoodExpander` looks for path
prefixes that have ``min_hits_per_prefix`` or more hits, then issues a
small follow-up SerpApi pass that asks for *more* pages under those
prefixes (using the prefix's own segment as the keyword and the parent
domain as the ``site:`` filter). The result is a budgeted retrieval boost
that follows wherever real interest landed in pass 1 — generic across
products.

Index-page link harvest
-----------------------
Catalogue / overview / index pages on vendor docs typically render the
entire sub-product menu as inline anchor links. SerpApi rarely surfaces
those individual leaf pages. :class:`IndexLinkHarvester` identifies
likely index pages from the post-fetch corpus (short path, "overview" /
"documentation" anchor segment, or short stripped-text length relative
to fetch window) and consumes :meth:`PageFetcher.harvest_links` to
enqueue inline same-domain descendants as additional candidate URLs for
the relevance scorer.

Both expansions are bounded (caps on follow-up SerpApi calls and on
harvested URLs) so the upper bound on cost is small and predictable.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Optional
from urllib.parse import urlparse

from claim_url._progress import progress
from claim_url.models import DomainSpec, RawHit, SearchResult
from claim_url.serp import SerpApiClient
from claim_url.utils import (
    canonicalize_url,
    dedupe_keep_order,
    domain_matches,
    normalize_domain,
    parse_domain_spec,
    url_matches_spec,
)

if TYPE_CHECKING:
    from claim_url.fetch import PageFetcher


LOG = logging.getLogger("claim-url-finder")


# Path segments that almost always indicate non-doc content. URLs whose path
# contains any of these are skipped when forming neighborhood-expansion
# candidate prefixes.
_PATH_DENYLIST: frozenset[str] = frozenset({
    "terms", "legal", "policies", "policy", "tos", "agreement",
    "agreements", "privacy", "pricing", "billing", "support-form",
    "changelog", "release-notes", "release_notes", "blog", "news",
    "events", "press", "careers", "contact", "about",
})

# Heuristic markers for "index / overview" pages whose body is mostly nav.
_INDEX_HINTS: frozenset[str] = frozenset({
    "documentation", "docs", "overview", "index", "products", "apis",
    "services", "solutions", "platform", "guide", "guides", "reference",
    "catalog",
})


@dataclass(slots=True)
class _PrefixBucket:
    domain: str
    prefix_segments: tuple[str, ...]
    urls: list[str] = field(default_factory=list)

    @property
    def prefix_path(self) -> str:
        if not self.prefix_segments:
            return "/"
        return "/" + "/".join(self.prefix_segments) + "/"


def _coerce_specs(domains) -> list[DomainSpec]:
    """Accept a list of DomainSpec or bare host strings; return DomainSpec list."""
    out: list[DomainSpec] = []
    for d in domains:
        if isinstance(d, DomainSpec):
            out.append(d)
            continue
        spec = parse_domain_spec(str(d))
        if spec is not None:
            out.append(spec)
    return out


class PathNeighborhoodExpander:
    """Issue a small follow-up SerpApi pass under hot path prefixes.

    Pass 1 hits are bucketed by ``(domain, first N path segments)``. Any
    bucket with at least ``min_hits_per_prefix`` URLs becomes a follow-up
    target. For each target, two queries are issued via SerpApi:

    1. ``"<deepest-segment-as-keyword> <product>" site:<domain>/<prefix>``
    2. ``"<vendor-anchor> <prefix-leaf>" site:<domain>/<prefix>``

    The total follow-up call count is capped at ``max_followups``.
    SerpApi disk caching deduplicates across runs, so re-runs cost zero.
    """

    def __init__(
        self,
        serp: SerpApiClient,
        *,
        prefix_segments: int = 3,
        min_hits_per_prefix: int = 2,
        max_followups: int = 12,
        per_followup: int = 10,
        anchors: Optional[Iterable[str]] = None,
        max_workers: int = 6,
    ) -> None:
        self._serp = serp
        self.prefix_segments = max(1, int(prefix_segments))
        self.min_hits_per_prefix = max(1, int(min_hits_per_prefix))
        self.max_followups = max(0, int(max_followups))
        self.per_followup = max(1, int(per_followup))
        self.anchors = [a for a in (anchors or []) if a]
        self.max_workers = max(1, int(max_workers))

    def expand(
        self,
        *,
        product: str,
        domains: list,
        existing_hits: list[RawHit],
        existing_urls: set[str],
        already_queried: set[tuple[str, str]],
    ) -> list[RawHit]:
        """Return supplementary :class:`RawHit` instances under hot prefixes.

        Args:
            product: Product name used as the brand anchor in follow-up queries.
            domains: Official domain list (results outside this set are dropped).
            existing_hits: Pass-1 hits — used to bucket by path prefix.
            existing_urls: URLs already in the candidate pool — skipped here.
            already_queried: ``(query, domain)`` pairs already issued — skipped.
        """
        if self.max_followups <= 0 or not existing_hits:
            return []

        buckets = self._build_buckets(existing_hits, domains)
        if not buckets:
            return []

        # Rank buckets by hit count desc — invest the budget where the
        # initial search already showed traction.
        buckets.sort(key=lambda b: len(b.urls), reverse=True)

        # Two-pass plan to enforce bucket fairness:
        # Pass 1: every qualifying bucket gets its FIRST follow-up query
        #         (up to max_followups). Niche / vertical prefixes with
        #         exactly min_hits hits would otherwise lose budget to
        #         popular prefixes with 10+ hits — fairness fixes that.
        # Pass 2: remaining budget filled with each bucket's SECOND query
        #         in the same bucket-rank order.
        bucket_queries: list[list[str]] = [
            self._queries_for_bucket(product, b) for b in buckets
        ]

        plan: list[tuple[str, str, _PrefixBucket]] = []

        def _try_add(q: str, bucket: _PrefixBucket) -> bool:
            if (q, bucket.domain) in already_queried:
                return False
            for existing_q, existing_d, _ in plan:
                if existing_q == q and existing_d == bucket.domain:
                    return False
            plan.append((q, bucket.domain, bucket))
            return True

        for q_idx in range(2):  # pass 1 = first query, pass 2 = second
            for b_idx, bucket in enumerate(buckets):
                if len(plan) >= self.max_followups:
                    break
                queries = bucket_queries[b_idx]
                if q_idx >= len(queries):
                    continue
                _try_add(queries[q_idx], bucket)
            if len(plan) >= self.max_followups:
                break

        if not plan:
            return []

        LOG.info(
            "Path-neighborhood expansion: %d follow-up queries across %d hot prefixes",
            len(plan), len({(b.domain, b.prefix_path) for _, _, b in plan}),
        )

        def _run(query: str, domain: str) -> list[SearchResult]:
            full_query = f"{query} site:{domain}"
            try:
                return self._serp.search(full_query, num=self.per_followup)
            except Exception as exc:
                LOG.warning(
                    "Neighborhood follow-up failed q=%r domain=%s error=%s",
                    query, domain, exc,
                )
                return []

        new_hits: list[RawHit] = []
        bar = progress(total=len(plan), desc="Neighborhood expansion", unit="q")
        try:
            workers = max(1, min(self.max_workers, len(plan)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_run, q, d): (q, d, b) for q, d, b in plan
                }
                for fut in as_completed(futures):
                    q, d, bucket = futures[fut]
                    results = fut.result()
                    for r in results:
                        new_hits.extend(
                            self._coerce_results_to_hits(
                                r, bucket=bucket, domain=d,
                                existing_urls=existing_urls,
                            )
                        )
                    bar.update(1)
        finally:
            bar.close()

        return new_hits

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _build_buckets(
        self, hits: list[RawHit], domains: list
    ) -> list[_PrefixBucket]:
        specs = _coerce_specs(domains)
        if not specs:
            return []

        by_key: dict[tuple[str, tuple[str, ...]], _PrefixBucket] = {}
        for hit in hits:
            try:
                parsed = urlparse(hit.url)
            except Exception:
                continue
            host = (parsed.netloc or "").lower()
            if not any(url_matches_spec(hit.url, s) for s in specs):
                continue
            segments = [s for s in (parsed.path or "/").split("/") if s]
            if not segments:
                continue
            if any(s.lower() in _PATH_DENYLIST for s in segments):
                continue
            prefix = tuple(segments[: self.prefix_segments])
            if not prefix:
                continue
            key = (host, prefix)
            bucket = by_key.get(key)
            if bucket is None:
                bucket = _PrefixBucket(domain=host, prefix_segments=prefix)
                by_key[key] = bucket
            if hit.url not in bucket.urls:
                bucket.urls.append(hit.url)

        return [
            b for b in by_key.values()
            if len(b.urls) >= self.min_hits_per_prefix
        ]

    def _queries_for_bucket(
        self, product: str, bucket: _PrefixBucket
    ) -> list[str]:
        """Generate up to two follow-up queries for *bucket*.

        The deepest path segment becomes the dominant keyword; product +
        a use-case anchor (when available) round it out so the query has
        a real lexical anchor instead of a bare keyword.
        """
        if not bucket.prefix_segments:
            return []
        leaf = self._humanize_segment(bucket.prefix_segments[-1])
        if not leaf:
            return []

        # Query 1: leaf + product. Generic; covers any vendor docs site.
        q1 = f"{leaf} {product}".strip()

        # Query 2: leaf + first available anchor. Adds use-case framing when
        # a UseCase has been classified upstream.
        if self.anchors:
            anchor = self.anchors[0]
            q2 = f"{leaf} {anchor}".strip()
        else:
            # Fall back to a second product-anchored angle: leaf + the parent
            # segment when one exists, e.g. "fleet-engine documentation".
            parent = (
                self._humanize_segment(bucket.prefix_segments[-2])
                if len(bucket.prefix_segments) >= 2 else ""
            )
            q2 = f"{leaf} {parent}".strip() or f"{product} {leaf}".strip()

        out = dedupe_keep_order([q1, q2])
        return [q for q in out if q]

    @staticmethod
    def _humanize_segment(segment: str) -> str:
        """Convert a URL segment ('fleet-engine_v2') to a search-friendly keyword."""
        cleaned = segment.replace("-", " ").replace("_", " ").strip()
        return " ".join(part for part in cleaned.split() if part)

    @staticmethod
    def _coerce_results_to_hits(
        result: SearchResult,
        *,
        bucket: _PrefixBucket,
        domain: str,
        existing_urls: set[str],
    ) -> list[RawHit]:
        url = canonicalize_url(result.url)
        if not url or url in existing_urls:
            return []
        try:
            parsed = urlparse(url)
        except Exception:
            return []
        host = (parsed.netloc or "").lower()
        if not domain_matches(host, domain):
            return []
        # Keep only descendants of the prefix path — that is the whole point
        # of the neighborhood expansion. A hit returning a different sub-tree
        # is not a neighbor.
        path = parsed.path or "/"
        prefix_path = bucket.prefix_path.rstrip("/")
        if prefix_path and not (
            path == prefix_path or path.startswith(prefix_path + "/")
        ):
            return []
        existing_urls.add(url)
        return [RawHit(
            url=url,
            title=result.title,
            snippet=result.snippet[:1000],
            element_id="EXPAND",  # synthetic element id; merged in scorer dedupe
            domain=host or domain,
        )]


# ---------------------------------------------------------------------------
# Index-page link harvester
# ---------------------------------------------------------------------------


class IndexLinkHarvester:
    """Harvest inline anchor hrefs from likely index pages already fetched.

    Catalogue / overview pages list dozens of sibling sub-pages inline.
    Surfacing those as additional candidate URLs requires zero extra
    SerpApi calls — the page bodies are already in
    :class:`PageFetcher`'s cache from the main fetch step.

    The harvest runs in up to ``max_passes`` iterations. Pass 1 processes
    the input hits and emits direct children. Pass 2+ re-processes any
    newly-discovered URL that itself looks like an index page, so that
    grandchildren of a deep index hierarchy are surfaced too. Common
    case: a ``/docs/`` parent links to ``/docs/foo/``, and ``/docs/foo/``
    links to the actual leaf pages — only a 2-pass harvest reaches the
    leaves.
    """

    def __init__(
        self,
        page_fetcher: "PageFetcher",
        *,
        max_links_per_index: int = 80,
        max_total_links: int = 200,
        min_index_path_segments: int = 1,
        max_index_path_segments: int = 3,
        max_passes: int = 2,
    ) -> None:
        self._fetcher = page_fetcher
        self.max_links_per_index = max(1, int(max_links_per_index))
        self.max_total_links = max(1, int(max_total_links))
        self.min_index_path_segments = max(0, int(min_index_path_segments))
        self.max_index_path_segments = max(
            self.min_index_path_segments, int(max_index_path_segments)
        )
        self.max_passes = max(1, int(max_passes))

    def harvest(
        self,
        *,
        hits: list[RawHit],
        existing_urls: set[str],
        domains: list,
        body_chars_threshold: int = 1500,
    ) -> list[RawHit]:
        """Return supplementary hits discovered from index-page anchor lists.

        Args:
            hits: pass-1 (and optional expansion) hits with bodies fetched.
            existing_urls: URLs already in the candidate pool — skipped.
            domains: official domain list (DomainSpec or bare host strings —
                links outside the host AND vendor path_prefix are dropped).
            body_chars_threshold: pages whose stripped body is shorter than
                this are flagged as likely index pages whose body is mostly
                nav HTML — exactly the case where harvesting links is a
                better signal than scoring the body.
        """
        specs = _coerce_specs(domains)
        if not specs:
            return []

        new_hits: list[RawHit] = []
        total_added = 0
        processed: set[str] = set()
        # Frontier seeded with the input hits; subsequent passes add freshly
        # discovered URLs that themselves look like index pages.
        frontier: list[tuple[str, RawHit | None]] = [(h.url, h) for h in hits]

        for pass_idx in range(self.max_passes):
            if total_added >= self.max_total_links or not frontier:
                break

            candidates: list[str] = []
            for url, hit in frontier:
                if url in processed:
                    continue
                processed.add(url)
                if not self._is_index_candidate(url, hit):
                    continue
                candidates.append(url)
            frontier = []

            if not candidates:
                break

            pass_added = 0
            for parent_url in candidates:
                if total_added >= self.max_total_links:
                    break
                links = self._fetcher.harvest_links(
                    parent_url, max_links=self.max_links_per_index
                )
                if not links:
                    continue
                parent_domain = (urlparse(parent_url).netloc or "").lower()
                for link in links:
                    link = canonicalize_url(link)
                    if link in existing_urls:
                        continue
                    link_host = (urlparse(link).netloc or "").lower()
                    if not any(url_matches_spec(link, s) for s in specs):
                        continue
                    # Tighten: only harvest links whose host equals the parent's
                    # host. Cross-subdomain links from index pages tend to point
                    # at marketing pages.
                    if link_host != parent_domain:
                        continue
                    existing_urls.add(link)
                    new_hit = RawHit(
                        url=link,
                        title=link.rsplit("/", 1)[-1] or parent_url,
                        snippet=f"Linked from index page {parent_url}",
                        element_id="INDEX",
                        domain=link_host,
                    )
                    new_hits.append(new_hit)
                    # Queue freshly-discovered URLs for the next pass — they
                    # may themselves be sub-index pages whose children matter
                    # (e.g. /mobility → /mobility/fleet-engine → ... leaves).
                    frontier.append((link, new_hit))
                    total_added += 1
                    pass_added += 1
                    if total_added >= self.max_total_links:
                        break

            LOG.debug(
                "Index-page link harvest pass=%d: %d candidates → %d new URLs",
                pass_idx + 1, len(candidates), pass_added,
            )

        if new_hits:
            LOG.info(
                "Index-page link harvest: %d new candidates over %d pass(es)",
                len(new_hits), min(pass_idx + 1, self.max_passes),
            )
        return new_hits

    def _is_index_candidate(self, url: str, hit: RawHit) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        path = parsed.path or "/"
        segments = [s for s in path.split("/") if s]
        depth = len(segments)
        if depth < self.min_index_path_segments or depth > self.max_index_path_segments:
            # Outside the configured depth window: not a typical index.
            # Allow short body override: if hit.body is suspiciously short
            # AND the URL ends in a known index-hint segment, accept.
            tail = (segments[-1] if segments else "").lower()
            if not (tail in _INDEX_HINTS and hit.body and len(hit.body) < 1500):
                return False

        if any(s.lower() in _PATH_DENYLIST for s in segments):
            return False
        # An index hint anywhere in the path is a strong positive signal.
        if any(s.lower() in _INDEX_HINTS for s in segments):
            return True
        # Otherwise: very short paths are likely landing pages.
        return depth <= 1


__all__ = ["PathNeighborhoodExpander", "IndexLinkHarvester"]
