from __future__ import annotations

import re
from typing import Iterable
from unittest.mock import MagicMock

from claim_url.agents.search import OfficialDomainSearch
from claim_url.models import ClaimElement, SearchResult


def _stub_serp(results_by_query: dict[str, list[SearchResult]]) -> MagicMock:
    mock = MagicMock()

    def _search(query: str, *, num: int = 5) -> list[SearchResult]:
        return results_by_query.get(query, [])

    mock.search.side_effect = _search
    return mock


def _result(url: str, title: str = "t", snippet: str = "s") -> SearchResult:
    return SearchResult(url=url, title=title, snippet=snippet)


def test_keeps_only_matching_domain_hits() -> None:
    serp = _stub_serp(
        {
            "search suggestions site:support.google.com": [
                _result("https://support.google.com/youtubetv/answer/1"),
                _result("https://random.example.com/blog"),
            ]
        }
    )
    element = ClaimElement(
        id="E1",
        label="search suggestions",
        keywords=["search"],
        search_queries=["search suggestions"],
    )
    searcher = OfficialDomainSearch(serp=serp, per_domain=5, sleep_seconds=0)
    hits = searcher.search(
        product="YouTube TV", elements=[element], domains=["support.google.com"]
    )
    assert [h.url for h in hits] == ["https://support.google.com/youtubetv/answer/1"]
    assert searcher.last_summary.api_calls == 1
    assert searcher.last_summary.hits_kept == 1


def test_dedupes_identical_query_domain_pair() -> None:
    serp = _stub_serp(
        {
            "search site:s.com": [_result("https://s.com/page-A")],
        }
    )
    element_a = ClaimElement(id="E1", label="a", keywords=["x"], search_queries=["search"])
    element_b = ClaimElement(id="E2", label="b", keywords=["y"], search_queries=["search"])
    searcher = OfficialDomainSearch(serp=serp, per_domain=5, sleep_seconds=0)
    searcher.search(product="P", elements=[element_a, element_b], domains=["s.com"])
    # Two elements both map to one (query, domain) pair → exactly one API call.
    assert searcher.last_summary.api_calls == 1
    assert searcher.last_summary.unique_queries == 1


def test_exclude_url_patterns_drops_matches() -> None:
    serp = _stub_serp(
        {
            "watch site:youtube.com": [
                _result("https://youtube.com/watch?v=123"),
                _result("https://youtube.com/help/article"),
            ]
        }
    )
    element = ClaimElement(id="E1", label="x", keywords=["w"], search_queries=["watch"])
    searcher = OfficialDomainSearch(
        serp=serp,
        per_domain=5,
        sleep_seconds=0,
        exclude_url_patterns=[re.compile(r"/watch\?")],
    )
    hits = searcher.search(product="P", elements=[element], domains=["youtube.com"])
    assert [h.url for h in hits] == ["https://youtube.com/help/article"]
    assert searcher.last_summary.excluded == 1


def test_collapses_locale_variants() -> None:
    """SerpApi returning ?hl=en and ?hl=en-GB for the same article must
    yield exactly one RawHit after canonicalization."""
    serp = _stub_serp(
        {
            "answer site:support.google.com": [
                _result("https://support.google.com/youtube/answer/6342839?hl=en"),
                _result("https://support.google.com/youtube/answer/6342839?hl=en-GB"),
                _result("https://support.google.com/youtube/answer/9999"),
            ]
        }
    )
    element = ClaimElement(id="E1", label="a", keywords=["x"], search_queries=["answer"])
    searcher = OfficialDomainSearch(serp=serp, per_domain=5, sleep_seconds=0)
    hits = searcher.search(
        product="P", elements=[element], domains=["support.google.com"]
    )
    urls = [h.url for h in hits]
    assert urls == [
        "https://support.google.com/youtube/answer/6342839",
        "https://support.google.com/youtube/answer/9999",
    ]
    assert searcher.last_summary.hits_kept == 2


def test_subdomain_acceptance_rule() -> None:
    """A target of 'youtube.com' must accept hits from 'tv.youtube.com'."""
    serp = _stub_serp(
        {
            "guide site:youtube.com": [_result("https://tv.youtube.com/guide")],
        }
    )
    element = ClaimElement(id="E1", label="x", keywords=["g"], search_queries=["guide"])
    searcher = OfficialDomainSearch(serp=serp, per_domain=5, sleep_seconds=0)
    hits = searcher.search(product="P", elements=[element], domains=["youtube.com"])
    assert len(hits) == 1
    assert hits[0].domain == "youtube.com"


def test_path_scoped_domain_issues_path_scoped_query() -> None:
    """Multi-tenant hosts must use ``site:host/path`` so other tenants
    on the same host don't match. Was: site:github.com matched every
    repo on the platform — this is the run17 Netflix Zuul fix.
    """
    serp = _stub_serp({
        "zuul filters site:github.com/Netflix": [
            _result("https://github.com/Netflix/zuul"),
            _result("https://github.com/Netflix/zuul/wiki/Filters"),
            # Same host, different tenant — must be filtered out by path.
            _result("https://github.com/akash-coded/spring-framework/discussions/164"),
        ],
    })
    element = ClaimElement(
        id="E1", label="filters", keywords=["zuul"],
        search_queries=["zuul filters"],
    )
    searcher = OfficialDomainSearch(serp=serp, per_domain=10, sleep_seconds=0)
    hits = searcher.search(
        product="Netflix Zuul",
        elements=[element],
        domains=["github.com/Netflix"],
    )
    urls = [h.url for h in hits]
    assert "https://github.com/Netflix/zuul" in urls
    assert "https://github.com/Netflix/zuul/wiki/Filters" in urls
    assert all("akash-coded" not in u for u in urls)
    assert all(h.domain == "github.com" for h in hits)


def test_path_scoped_search_rejects_third_party_tenant() -> None:
    """SerpApi may leak third-party tenants even with a path-scoped
    site: query (e.g. wiki references). The post-filter must still drop
    URLs whose path doesn't sit under the vendor path_prefix.
    """
    serp = _stub_serp({
        "zuul site:github.com/Netflix": [
            _result("https://github.com/xinrong-meng/knowledge-sharing/blob/master/24.%20Zuul%20Study.md"),
        ],
    })
    element = ClaimElement(id="E1", label="z", keywords=["z"], search_queries=["zuul"])
    searcher = OfficialDomainSearch(serp=serp, per_domain=5, sleep_seconds=0)
    hits = searcher.search(
        product="Netflix Zuul",
        elements=[element],
        domains=["github.com/Netflix"],
    )
    assert hits == []
