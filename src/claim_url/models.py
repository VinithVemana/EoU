"""Dataclass models flowing through the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ClaimElement:
    """A single discrete technical limitation extracted from a patent claim."""

    id: str
    label: str
    keywords: list[str]
    search_queries: list[str] = field(default_factory=list)

    def keyword_query(self, product: str, max_keywords: int = 4) -> str:
        """Build the keyword-only fallback query.

        Used when :class:`~claim_url.agents.rewriter.QueryRewriteAgent`
        produced no rewritten query for this element. The product is
        quoted (anchor); keywords stay unquoted to allow partial matching.
        """
        terms: list[str] = [f'"{product}"']
        for keyword in self.keywords[:max_keywords]:
            keyword = keyword.strip()
            if keyword:
                terms.append(keyword)
        return " ".join(terms)

    def queries(self, product: str, max_keywords: int = 4) -> list[str]:
        """Return rewritten queries if any, else the keyword fallback."""
        cleaned = [q.strip() for q in self.search_queries if q and q.strip()]
        if cleaned:
            return cleaned
        return [self.keyword_query(product, max_keywords=max_keywords)]


@dataclass(slots=True)
class DomainCandidate:
    domain: str
    confidence: float
    rationale: str = ""
    source_urls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchResult:
    url: str
    title: str
    snippet: str


@dataclass(slots=True)
class RawHit:
    url: str
    title: str
    snippet: str
    element_id: str
    domain: str
    body: str = ""


@dataclass(slots=True)
class ScoredURL:
    url: str
    title: str
    snippet: str
    score: float
    matched_elements: list[str] = field(default_factory=list)
    rationale: str = ""


@dataclass(slots=True)
class FinderResult:
    product: str
    domains: list[DomainCandidate]
    elements: list[ClaimElement]
    urls: list[ScoredURL]

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


__all__ = [
    "ClaimElement",
    "DomainCandidate",
    "FinderResult",
    "RawHit",
    "ScoredURL",
    "SearchResult",
]
