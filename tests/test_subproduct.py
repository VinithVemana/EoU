from __future__ import annotations

import json
from unittest.mock import MagicMock

from claim_url.agents.subproduct import SubProduct, SubProductAgent
from claim_url.models import DomainCandidate, SearchResult


def _llm(payload: str) -> MagicMock:
    llm = MagicMock()
    llm.complete.return_value = payload
    return llm


def _serp(results_by_query: dict[str, list[SearchResult]] | None = None) -> MagicMock:
    """Return a SerpApiClient mock that returns canned results per query, or empty."""
    serp = MagicMock()
    canned = results_by_query or {}

    def _search(query: str, *, num: int = 5) -> list[SearchResult]:
        return canned.get(query, [])

    serp.search.side_effect = _search
    return serp


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
    agent = SubProductAgent(llm=_llm(payload), two_step_harvest=False)
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
    agent = SubProductAgent(llm=_llm(payload), two_step_harvest=False)
    result = agent.discover(product="P", claim="c", domains=[])
    assert [sp.name for sp in result] == ["Foo", "Bar"]


def test_subproduct_agent_handles_invalid_payload() -> None:
    agent = SubProductAgent(llm=_llm("not json at all"), two_step_harvest=False)
    result = agent.discover(product="P", claim="c", domains=[])
    assert result == []


def test_subproduct_agent_handles_missing_subproducts_key() -> None:
    agent = SubProductAgent(
        llm=_llm(json.dumps({"foo": "bar"})), two_step_harvest=False
    )
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
    agent = SubProductAgent(
        llm=_llm(payload), max_subproducts=3, two_step_harvest=False,
    )
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
    agent = SubProductAgent(llm=_llm(payload), two_step_harvest=False)
    result = agent.discover(product="P", claim="c", domains=[])
    assert [sp.name for sp in result] == ["Valid"]


def test_subproduct_agent_passes_serp_evidence_to_llm() -> None:
    """When SerpApi is provided, catalogue evidence is enumerated and embedded
    in the LLM prompt — this is the evidence-based path."""
    serp = _serp({
        "P products list": [
            SearchResult(url="https://p.example.com/products/foo",
                         title="Foo Product", snippet="Foo product overview"),
            SearchResult(url="https://p.example.com/products/bar-engine",
                         title="Bar Engine", snippet="Bar Engine docs index"),
        ],
    })
    payload = json.dumps(
        {"subproducts": [
            {"name": "Bar Engine", "vocabulary": ["bar"], "rationale": "from evidence"}
        ]}
    )
    llm = _llm(payload)
    agent = SubProductAgent(
        llm=llm, serp=serp, probe_results_per_query=2, two_step_harvest=False,
    )
    result = agent.discover(
        product="P", claim="claim", domains=[_domain("p.example.com")]
    )
    assert [sp.name for sp in result] == ["Bar Engine"]
    # Confirm prompt actually carried the SerpApi evidence.
    sent_prompt = llm.complete.call_args.kwargs["prompt"]
    assert "Bar Engine" in sent_prompt
    assert "p.example.com/products/bar-engine" in sent_prompt


def test_subproduct_agent_works_without_serp() -> None:
    """When SerpApi is not provided, agent runs in memory-only mode (no
    evidence) and still returns results from the LLM."""
    payload = json.dumps(
        {"subproducts": [{"name": "X", "vocabulary": [], "rationale": ""}]}
    )
    agent = SubProductAgent(llm=_llm(payload), serp=None, two_step_harvest=False)
    result = agent.discover(product="P", claim="c", domains=[])
    assert [sp.name for sp in result] == ["X"]


def test_subproduct_agent_fetches_catalogue_pages() -> None:
    """When a PageFetcher is provided, the agent fetches the highest-authority
    catalogue/overview pages from the SerpApi evidence and embeds their body
    excerpts in the LLM prompt — niche sub-products typically appear inline
    on those index pages but never in SerpApi titles alone."""
    serp = _serp({
        "P products list": [
            SearchResult(url="https://p.example.com/products",
                         title="Products Index", snippet="catalog overview"),
            SearchResult(url="https://p.example.com/some/deep/nested/legal/terms",
                         title="Terms", snippet="legal page"),
            SearchResult(url="https://other-third-party.example.org/products/p",
                         title="3rd party blog", snippet="external"),
        ],
    })

    fetcher = MagicMock()
    fetcher.fetch_many.return_value = {
        "https://p.example.com/products":
            "Foo API. Bar Engine. Baz SDK. Niche Mobility Service. Driver Tools.",
    }

    payload = json.dumps(
        {"subproducts": [
            {"name": "Niche Mobility Service", "vocabulary": ["mobility"],
             "rationale": "harvested from catalogue page body"}
        ]}
    )
    llm = _llm(payload)

    agent = SubProductAgent(
        llm=llm, serp=serp, page_fetcher=fetcher,
        probe_results_per_query=3, max_catalogue_pages=2,
        two_step_harvest=False,
    )
    result = agent.discover(
        product="P", claim="some claim", domains=[_domain("p.example.com")]
    )
    # 1) page fetcher was invoked with the official-domain URL only
    fetched = fetcher.fetch_many.call_args.args[0]
    assert "https://p.example.com/products" in fetched
    # 3rd-party domains are filtered out of catalogue candidates
    assert all("third-party" not in u for u in fetched)
    # 2) prompt embedded the page body so the LLM can harvest from it
    sent_prompt = llm.complete.call_args.kwargs["prompt"]
    assert "Niche Mobility Service" in sent_prompt
    assert "Driver Tools" in sent_prompt
    # 3) the LLM picks something from the body
    assert [sp.name for sp in result] == ["Niche Mobility Service"]


def test_subproduct_agent_skips_catalogue_fetch_without_fetcher() -> None:
    """No PageFetcher → no catalogue-page fetch. Probe still runs evidence-only."""
    serp = _serp({"P products list": [
        SearchResult(url="https://p.example.com/products", title="Idx", snippet=""),
    ]})
    payload = json.dumps({"subproducts": []})
    agent = SubProductAgent(
        llm=_llm(payload), serp=serp, page_fetcher=None, two_step_harvest=False,
    )
    agent.discover(product="P", claim="c", domains=[_domain("p.example.com")])
    sent_prompt = agent._llm.complete.call_args.kwargs["prompt"]
    assert "no catalogue pages fetched" in sent_prompt


def test_subproduct_agent_dedupes_evidence_by_url_and_caps() -> None:
    """Evidence list dedupes by URL across queries and caps at max_evidence_items."""
    same_url = SearchResult(url="https://p.example.com/x", title="X", snippet="")
    serp = _serp({
        "P products list": [same_url, same_url, same_url],
        "P all APIs": [same_url],
    })
    payload = json.dumps({"subproducts": []})
    agent = SubProductAgent(
        llm=_llm(payload), serp=serp, max_evidence_items=10,
        two_step_harvest=False,
    )
    agent.discover(product="P", claim="c", domains=[])
    # Only one URL in evidence after dedupe — confirm by inspecting prompt.
    sent_prompt = agent._llm.complete.call_args.kwargs["prompt"]
    assert sent_prompt.count("p.example.com/x") == 1


# ---------------------------------------------------------------------------
# Two-step harvest tests
# ---------------------------------------------------------------------------

def _llm_two_step(enumerate_payload: str, filter_payload: str) -> MagicMock:
    """Mock LLM that returns the enumerate payload first, filter payload second."""
    llm = MagicMock()
    llm.complete.side_effect = [enumerate_payload, filter_payload]
    return llm


def test_two_step_harvest_runs_enumerate_then_filter() -> None:
    """Default path: two LLM calls. Enumeration carries every visible
    surface; filter ranks the subset most relevant to the claim."""
    enumerate_payload = json.dumps({
        "subproducts": [
            {"name": "Geocoding API", "vocabulary": ["geocode"], "evidenced": True},
            {"name": "Routes API", "vocabulary": ["route"], "evidenced": True},
            {"name": "Fleet Engine", "vocabulary": ["fleet", "dispatch"], "evidenced": True},
            {"name": "Maps Embed API", "vocabulary": ["embed"], "evidenced": True},
        ]
    })
    filter_payload = json.dumps({
        "subproducts": [
            {"name": "Fleet Engine",
             "vocabulary": ["fleet", "dispatch"],
             "rationale": "matches the dispatch claim"},
            {"name": "Routes API",
             "vocabulary": ["route"],
             "rationale": "secondary"},
        ]
    })

    serp = _serp({"P products list": [
        SearchResult(url="https://p.example.com/products",
                     title="Catalog", snippet="all surfaces"),
    ]})
    llm = _llm_two_step(enumerate_payload, filter_payload)
    agent = SubProductAgent(llm=llm, serp=serp, max_subproducts=5)
    result = agent.discover(
        product="P", claim="dispatch claim",
        domains=[_domain("p.example.com")],
    )

    # Two LLM calls happened.
    assert llm.complete.call_count == 2
    # Filter step picked Fleet Engine as the top entry — niche surface beats popular ones.
    assert [sp.name for sp in result] == ["Fleet Engine", "Routes API"]
    # Filter step prompt includes the full enumeration so the LLM knows the candidate pool.
    filter_prompt = llm.complete.call_args_list[1].kwargs["prompt"]
    assert "Fleet Engine" in filter_prompt
    assert "Geocoding API" in filter_prompt
    assert "Maps Embed API" in filter_prompt


def test_two_step_harvest_falls_back_when_enumeration_empty() -> None:
    """If enumerate step returns nothing, fall back to single-step prompt
    instead of failing silently."""
    enumerate_payload = json.dumps({"subproducts": []})
    fallback_payload = json.dumps(
        {"subproducts": [{"name": "Fallback", "vocabulary": [], "rationale": "ok"}]}
    )
    llm = MagicMock()
    llm.complete.side_effect = [enumerate_payload, fallback_payload]

    agent = SubProductAgent(llm=llm)
    result = agent.discover(product="P", claim="c", domains=[])
    # Two calls: failed enumerate + single-step fallback.
    assert llm.complete.call_count == 2
    assert [sp.name for sp in result] == ["Fallback"]


def test_two_step_harvest_recovers_vocabulary_when_filter_drops_it() -> None:
    """If filter step omits vocabulary for a surface, the agent restores it
    from the enumeration so downstream stages still get distinctive tokens."""
    enumerate_payload = json.dumps({
        "subproducts": [
            {"name": "Fleet Engine",
             "vocabulary": ["fleet", "dispatch", "driver"],
             "evidenced": True},
        ]
    })
    filter_payload = json.dumps({
        "subproducts": [
            {"name": "Fleet Engine", "vocabulary": [], "rationale": "match"}
        ]
    })
    llm = _llm_two_step(enumerate_payload, filter_payload)
    agent = SubProductAgent(llm=llm)
    result = agent.discover(product="P", claim="c", domains=[])
    assert result[0].name == "Fleet Engine"
    assert result[0].vocabulary == ["fleet", "dispatch", "driver"]


def test_two_step_harvest_passes_use_case_to_filter_prompt() -> None:
    """When a UseCase is provided, its label + anchors appear in the filter
    prompt so the LLM can prefer surfaces matching the claim's domain."""
    from claim_url.agents.use_case import UseCase

    enumerate_payload = json.dumps({
        "subproducts": [
            {"name": "Fleet Engine", "vocabulary": ["fleet"], "evidenced": True},
        ]
    })
    filter_payload = json.dumps({
        "subproducts": [
            {"name": "Fleet Engine", "vocabulary": ["fleet"], "rationale": "matches"}
        ]
    })
    llm = _llm_two_step(enumerate_payload, filter_payload)
    agent = SubProductAgent(llm=llm)
    use_case = UseCase(
        use_case="vehicle dispatch",
        anchors=["dispatch", "fleet", "driver"],
        alternative_use_cases=["asset tracking"],
    )
    agent.discover(product="P", claim="c", domains=[], use_case=use_case)
    filter_prompt = llm.complete.call_args_list[1].kwargs["prompt"]
    assert "vehicle dispatch" in filter_prompt
    assert "dispatch" in filter_prompt
    assert "asset tracking" in filter_prompt
