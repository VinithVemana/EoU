from __future__ import annotations

from claim_url.models import ClaimElement, FinderResult


class TestClaimElementQueries:
    def test_uses_rewritten_when_present(self) -> None:
        element = ClaimElement(
            id="E1",
            label="presenting recommendations",
            keywords=["recommend", "lineup"],
            search_queries=["youtube tv recommendations", "what to watch lineup"],
        )
        assert element.queries("YouTube TV") == [
            "youtube tv recommendations",
            "what to watch lineup",
        ]

    def test_falls_back_to_keyword_query(self) -> None:
        element = ClaimElement(
            id="E1",
            label="some limitation",
            keywords=["alpha", "beta", "gamma"],
        )
        queries = element.queries("X")
        assert queries == ['"X" alpha beta gamma']

    def test_keyword_query_quotes_product_only(self) -> None:
        element = ClaimElement(id="E1", label="x", keywords=["a", "b"])
        assert element.keyword_query("Acme Corp") == '"Acme Corp" a b'

    def test_keyword_query_truncates_keywords(self) -> None:
        element = ClaimElement(id="E1", label="x", keywords=["k1", "k2", "k3", "k4", "k5"])
        # Default max_keywords=4, so k5 dropped.
        assert element.keyword_query("P") == '"P" k1 k2 k3 k4'

    def test_blank_rewritten_queries_treated_as_missing(self) -> None:
        element = ClaimElement(
            id="E1",
            label="x",
            keywords=["k"],
            search_queries=["", "   "],
        )
        assert element.queries("P") == ['"P" k']


class TestFinderResultRoundtrip:
    def test_to_dict_round_trips(self) -> None:
        element = ClaimElement(id="E1", label="hello", keywords=["k"])
        result = FinderResult(product="P", domains=[], elements=[element], urls=[])
        data = result.to_dict()
        assert data["product"] == "P"
        assert data["elements"][0]["id"] == "E1"
