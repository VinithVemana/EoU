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
