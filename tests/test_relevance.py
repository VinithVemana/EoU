from __future__ import annotations

import json
from unittest.mock import MagicMock

from claim_url.agents.relevance import RelevanceCheckingAgent
from claim_url.models import ClaimElement, RawHit


def _scripted_llm(payloads: list[dict]) -> MagicMock:
    """Stub LLMClient.complete returning each payload (as JSON) in order."""
    llm = MagicMock()
    llm.complete.side_effect = [json.dumps(p) for p in payloads]
    return llm


def _hit(url: str, *, body: str = "") -> RawHit:
    return RawHit(
        url=url, title=f"title-{url}", snippet="snip", element_id="E1", domain="ex.com", body=body
    )


def test_dedupes_across_batches_keeping_highest_score() -> None:
    llm = _scripted_llm(
        [
            {"ranked": [{"url": "https://ex.com/a", "score": 0.4, "matched_elements": ["E1"]}]},
            {"ranked": [{"url": "https://ex.com/a", "score": 0.9, "matched_elements": ["E2"]}]},
        ]
    )
    agent = RelevanceCheckingAgent(llm=llm, max_candidates_per_batch=1)
    out = agent.score(
        product="P",
        claim="claim text",
        elements=[ClaimElement(id="E1", label="x", keywords=[])],
        hits=[_hit("https://ex.com/a"), _hit("https://ex.com/b")],
    )
    by_url = {s.url: s for s in out}
    # Only "/a" was rescored — "/b" got no payload, dropped.
    assert by_url["https://ex.com/a"].score == 0.9
    assert by_url["https://ex.com/a"].matched_elements == ["E2"]


def test_tied_scores_merge_matched_elements_and_rationales() -> None:
    llm = _scripted_llm(
        [
            {"ranked": [
                {"url": "https://ex.com/a", "score": 0.5, "matched_elements": ["E1"], "rationale": "r1"}
            ]},
            {"ranked": [
                {"url": "https://ex.com/a", "score": 0.5, "matched_elements": ["E2"], "rationale": "r2"}
            ]},
        ]
    )
    agent = RelevanceCheckingAgent(llm=llm, max_candidates_per_batch=1)
    out = agent.score(
        product="P",
        claim="claim text",
        elements=[ClaimElement(id="E1", label="x", keywords=[])],
        hits=[_hit("https://ex.com/a"), _hit("https://ex.com/b")],
    )
    [scored] = [s for s in out if s.url == "https://ex.com/a"]
    assert scored.matched_elements == ["E1", "E2"]
    assert "r1" in scored.rationale and "r2" in scored.rationale


def test_drops_zero_scores() -> None:
    llm = _scripted_llm(
        [{"ranked": [{"url": "https://ex.com/a", "score": 0.0, "matched_elements": []}]}]
    )
    agent = RelevanceCheckingAgent(llm=llm, max_candidates_per_batch=10)
    out = agent.score(
        product="P",
        claim="c",
        elements=[ClaimElement(id="E1", label="x", keywords=[])],
        hits=[_hit("https://ex.com/a")],
    )
    assert out == []


def test_batch_failure_does_not_abort_pipeline() -> None:
    llm = MagicMock()
    llm.complete.side_effect = [
        RuntimeError("boom"),
        json.dumps({"ranked": [{"url": "https://ex.com/b", "score": 0.7, "matched_elements": []}]}),
    ]
    agent = RelevanceCheckingAgent(llm=llm, max_candidates_per_batch=1)
    out = agent.score(
        product="P",
        claim="c",
        elements=[ClaimElement(id="E1", label="x", keywords=[])],
        hits=[_hit("https://ex.com/a"), _hit("https://ex.com/b")],
    )
    assert [s.url for s in out] == ["https://ex.com/b"]


def test_empty_hits_short_circuits() -> None:
    llm = MagicMock()
    agent = RelevanceCheckingAgent(llm=llm)
    assert agent.score(product="P", claim="c", elements=[], hits=[]) == []
    llm.complete.assert_not_called()
