from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from claim_url.agents.extractor import ClaimElementExtractor
from claim_url.errors import ClaimURLError


def _llm_returning(payload: dict | str) -> MagicMock:
    llm = MagicMock()
    llm.complete.return_value = payload if isinstance(payload, str) else json.dumps(payload)
    return llm


def test_extracts_clean_elements() -> None:
    llm = _llm_returning(
        {
            "elements": [
                {"id": "E1", "label": "first", "keywords": ["a", "b"]},
                {"id": "E2", "label": "second", "keywords": ["c"]},
            ]
        }
    )
    elements = ClaimElementExtractor(llm).extract("any claim")
    assert [e.id for e in elements] == ["E1", "E2"]
    assert elements[0].keywords == ["a", "b"]


def test_falls_back_to_label_words_when_keywords_missing() -> None:
    llm = _llm_returning(
        {"elements": [{"id": "E1", "label": "search suggestions and autocomplete"}]}
    )
    [element] = ClaimElementExtractor(llm).extract("any claim")
    assert element.keywords == ["search", "suggestions", "and", "autocomplete"]


def test_assigns_default_id_when_missing() -> None:
    llm = _llm_returning({"elements": [{"label": "no id here", "keywords": ["k"]}]})
    [element] = ClaimElementExtractor(llm).extract("any claim")
    assert element.id == "E1"


def test_drops_items_without_label() -> None:
    llm = _llm_returning(
        {"elements": [{"id": "E1", "label": ""}, {"id": "E2", "label": "ok", "keywords": ["k"]}]}
    )
    elements = ClaimElementExtractor(llm).extract("any claim")
    assert [e.id for e in elements] == ["E2"]


def test_raises_when_llm_returns_no_elements() -> None:
    llm = _llm_returning({"elements": []})
    with pytest.raises(ClaimURLError):
        ClaimElementExtractor(llm).extract("any claim")


def test_blank_claim_rejected() -> None:
    llm = MagicMock()
    with pytest.raises(ValueError):
        ClaimElementExtractor(llm).extract("   ")
    llm.complete.assert_not_called()
