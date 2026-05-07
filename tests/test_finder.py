from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from claim_url.finder import ClaimURLFinder
from claim_url.models import SearchResult


def _llm_with_responses(*responses: str) -> MagicMock:
    llm = MagicMock()
    llm.complete.side_effect = list(responses)
    return llm


def _serp_with(results: dict[str, list[SearchResult]]) -> MagicMock:
    serp = MagicMock()

    def _search(query: str, *, num: int = 5) -> list[SearchResult]:
        return results.get(query, [])

    serp.search.side_effect = _search
    return serp


def test_full_pipeline_with_domain_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end run: skip Agent 1 via override, mock LLM + Serp."""

    extract_payload = json.dumps(
        {
            "elements": [
                {"id": "E1", "label": "search suggestions", "keywords": ["search"]},
            ]
        }
    )
    rewrite_payload = json.dumps(
        {"elements": [{"id": "E1", "queries": ["youtube tv search suggestions"]}]}
    )
    relevance_payload = json.dumps(
        {
            "ranked": [
                {
                    "url": "https://support.google.com/youtubetv/answer/123",
                    "score": 0.95,
                    "matched_elements": ["E1"],
                    "rationale": "describes search suggestions",
                }
            ]
        }
    )

    llm = _llm_with_responses(extract_payload, rewrite_payload, relevance_payload)
    serp = _serp_with(
        {
            "youtube tv search suggestions site:support.google.com": [
                SearchResult(
                    url="https://support.google.com/youtubetv/answer/123",
                    title="Search suggestions",
                    snippet="...",
                )
            ]
        }
    )

    finder = ClaimURLFinder(
        llm=llm,
        serp=serp,
        max_domains=2,
        per_domain=3,
        max_candidates_per_batch=10,
        queries_per_element=1,
        page_fetcher=None,
        enable_subproduct_probe=False,
        enable_use_case_classification=False,
        enable_path_expansion=False,
        enable_index_link_harvest=False,
    )
    result = finder.run(
        claim="A computer-implemented method for receiving incremental keystrokes...",
        product="YouTube TV",
        top_k=5,
        domain_override=["support.google.com"],
    )

    assert result.product == "YouTube TV"
    assert [d.domain for d in result.domains] == ["support.google.com"]
    assert [e.id for e in result.elements] == ["E1"]
    assert len(result.urls) == 1
    assert result.urls[0].score == pytest.approx(0.95)


def test_empty_search_short_circuits_relevance(monkeypatch: pytest.MonkeyPatch) -> None:
    extract_payload = json.dumps(
        {"elements": [{"id": "E1", "label": "x", "keywords": ["k"]}]}
    )
    rewrite_payload = json.dumps({"elements": [{"id": "E1", "queries": ["k"]}]})
    llm = _llm_with_responses(extract_payload, rewrite_payload)
    serp = _serp_with({})  # always empty

    finder = ClaimURLFinder(
        llm=llm,
        serp=serp,
        queries_per_element=1,
        enable_subproduct_probe=False,
        enable_use_case_classification=False,
        enable_path_expansion=False,
        enable_index_link_harvest=False,
    )
    result = finder.run(
        claim="claim text",
        product="P",
        top_k=5,
        domain_override=["example.com"],
    )
    assert result.urls == []
    # Only two LLM calls: extractor + rewriter. Relevance never invoked.
    assert llm.complete.call_count == 2


def test_blank_inputs_raise() -> None:
    finder = ClaimURLFinder(llm=MagicMock(), serp=MagicMock())
    with pytest.raises(ValueError):
        finder.run(claim="", product="P")
    with pytest.raises(ValueError):
        finder.run(claim="x", product=" ")


def test_use_case_classification_runs_when_enabled() -> None:
    """End-to-end with use-case classification on: extra LLM call inserted
    between extractor and rewriter and its anchors flow into the rewriter."""
    extract_payload = json.dumps(
        {"elements": [{"id": "E1", "label": "search suggestions", "keywords": ["search"]}]}
    )
    use_case_payload = json.dumps(
        {"use_case": "on-device autocomplete",
         "anchors": ["autocomplete", "suggest"],
         "alternative_use_cases": []}
    )
    rewrite_payload = json.dumps(
        {"elements": [{"id": "E1", "queries": ["youtube tv autocomplete"]}]}
    )
    relevance_payload = json.dumps(
        {"ranked": [{"url": "https://support.google.com/youtubetv/answer/1",
                     "score": 0.9, "matched_elements": ["E1"], "rationale": "ok"}]}
    )
    llm = _llm_with_responses(
        extract_payload, use_case_payload, rewrite_payload, relevance_payload,
    )
    serp = _serp_with({
        "youtube tv autocomplete site:support.google.com": [
            SearchResult(
                url="https://support.google.com/youtubetv/answer/1",
                title="autocomplete", snippet="...",
            ),
        ],
    })

    finder = ClaimURLFinder(
        llm=llm,
        serp=serp,
        queries_per_element=1,
        page_fetcher=None,
        enable_subproduct_probe=False,
        enable_use_case_classification=True,
        enable_path_expansion=False,
        enable_index_link_harvest=False,
    )
    result = finder.run(
        claim="receive incremental keystrokes",
        product="YouTube TV",
        top_k=5,
        domain_override=["support.google.com"],
    )
    assert llm.complete.call_count == 4
    assert len(result.urls) == 1

    # Rewriter (3rd call) prompt should carry use-case anchors.
    rewriter_prompt = llm.complete.call_args_list[2].kwargs["prompt"]
    assert "autocomplete" in rewriter_prompt
    assert "on-device autocomplete" in rewriter_prompt


def test_path_scoped_domain_override_filters_third_party_github() -> None:
    """End-to-end: --domains 'github.com/Netflix' must reject hits under
    other GitHub orgs. Reproduces the run17 Netflix Zuul bug fix.
    """
    extract_payload = json.dumps(
        {"elements": [{"id": "E1", "label": "filters", "keywords": ["filter"]}]}
    )
    rewrite_payload = json.dumps(
        {"elements": [{"id": "E1", "queries": ["zuul filters"]}]}
    )
    relevance_payload = json.dumps(
        {"ranked": [{
            "url": "https://github.com/Netflix/zuul/wiki/Filters",
            "score": 0.92,
            "matched_elements": ["E1"],
            "rationale": "Netflix Zuul wiki filters page",
        }]}
    )
    llm = _llm_with_responses(extract_payload, rewrite_payload, relevance_payload)
    serp = _serp_with({
        "zuul filters site:github.com/Netflix": [
            SearchResult(
                url="https://github.com/Netflix/zuul/wiki/Filters",
                title="Filters", snippet="...",
            ),
            SearchResult(
                url="https://github.com/akash-coded/spring-framework/discussions/164",
                title="Spring Zuul discussion", snippet="...",
            ),
            SearchResult(
                url="https://github.com/xinrong-meng/knowledge-sharing/blob/master/24.%20Zuul%20Study.md",
                title="Zuul Study", snippet="...",
            ),
        ],
    })

    finder = ClaimURLFinder(
        llm=llm,
        serp=serp,
        queries_per_element=1,
        page_fetcher=None,
        enable_subproduct_probe=False,
        enable_use_case_classification=False,
        enable_path_expansion=False,
        enable_index_link_harvest=False,
    )
    result = finder.run(
        claim="A method comprising filtering at the edge proxy.",
        product="Netflix Zuul",
        top_k=5,
        domain_override=["github.com/Netflix"],
    )
    urls = [u.url for u in result.urls]
    # Only the Netflix-org URL survives the path-prefix filter.
    assert urls == ["https://github.com/Netflix/zuul/wiki/Filters"]
    # The DomainCandidate carries the vendor path so trace artifacts and the
    # CLI display show "github.com/Netflix" rather than bare "github.com".
    assert result.domains[0].domain == "github.com"
    assert result.domains[0].path_prefix == "/Netflix"
    assert result.domains[0].display() == "github.com/Netflix"


def test_invalid_domain_override_raises() -> None:
    finder = ClaimURLFinder(
        llm=MagicMock(), serp=MagicMock(),
        enable_subproduct_probe=False,
        enable_use_case_classification=False,
        enable_path_expansion=False,
        enable_index_link_harvest=False,
    )
    with pytest.raises(ValueError):
        finder.run(claim="x", product="P", domain_override=["///"])
