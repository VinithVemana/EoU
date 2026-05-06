from __future__ import annotations

import json
from unittest.mock import MagicMock

from claim_url.agents.subproduct import SubProduct, SubProductAgent
from claim_url.models import DomainCandidate


def _llm(payload: str) -> MagicMock:
    llm = MagicMock()
    llm.complete.return_value = payload
    return llm


def _domain(name: str) -> DomainCandidate:
    return DomainCandidate(domain=name, confidence=0.9, rationale="r", source_urls=[])


def test_subproduct_agent_parses_response() -> None:
    payload = json.dumps(
        {
            "subproducts": [
                {
                    "name": "Foo API",
                    "vocabulary": ["foo", "bar", "baz"],
                    "rationale": "matches the claim",
                },
                {
                    "name": "Quux SDK",
                    "vocabulary": ["quux", "qux"],
                    "rationale": "also matches",
                },
            ]
        }
    )
    agent = SubProductAgent(llm=_llm(payload))
    result = agent.discover(
        product="P", claim="some claim", domains=[_domain("p.example.com")]
    )
    assert [sp.name for sp in result] == ["Foo API", "Quux SDK"]
    assert result[0].vocabulary == ["foo", "bar", "baz"]


def test_subproduct_agent_dedupes_by_name() -> None:
    payload = json.dumps(
        {
            "subproducts": [
                {"name": "Foo", "vocabulary": ["a"], "rationale": "x"},
                {"name": "foo", "vocabulary": ["b"], "rationale": "y"},  # case-insensitive dup
                {"name": "Bar", "vocabulary": ["c"], "rationale": "z"},
            ]
        }
    )
    agent = SubProductAgent(llm=_llm(payload))
    result = agent.discover(product="P", claim="c", domains=[])
    assert [sp.name for sp in result] == ["Foo", "Bar"]


def test_subproduct_agent_handles_invalid_payload() -> None:
    agent = SubProductAgent(llm=_llm("not json at all"))
    result = agent.discover(product="P", claim="c", domains=[])
    assert result == []


def test_subproduct_agent_handles_missing_subproducts_key() -> None:
    agent = SubProductAgent(llm=_llm(json.dumps({"foo": "bar"})))
    result = agent.discover(product="P", claim="c", domains=[])
    assert result == []


def test_subproduct_agent_caps_at_max() -> None:
    payload = json.dumps(
        {
            "subproducts": [
                {"name": f"S{i}", "vocabulary": [], "rationale": ""}
                for i in range(20)
            ]
        }
    )
    agent = SubProductAgent(llm=_llm(payload), max_subproducts=3)
    result = agent.discover(product="P", claim="c", domains=[])
    assert len(result) == 3
    assert [sp.name for sp in result] == ["S0", "S1", "S2"]


def test_subproduct_agent_skips_entries_without_name() -> None:
    payload = json.dumps(
        {
            "subproducts": [
                {"vocabulary": ["a"], "rationale": "no name"},
                {"name": "", "vocabulary": ["b"]},
                {"name": "Valid", "vocabulary": ["c"]},
            ]
        }
    )
    agent = SubProductAgent(llm=_llm(payload))
    result = agent.discover(product="P", claim="c", domains=[])
    assert [sp.name for sp in result] == ["Valid"]
