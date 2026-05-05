from __future__ import annotations

import pytest

from claim_url.utils import (
    chunked,
    dedupe_keep_order,
    domain_matches,
    normalize_domain,
    parse_json_object,
    strip_markdown_json,
)


class TestNormalizeDomain:
    def test_full_url_with_subdomain(self) -> None:
        assert normalize_domain("https://support.google.com/youtube") == "support.google.com"

    def test_strips_www(self) -> None:
        assert normalize_domain("www.YouTube.COM") == "youtube.com"

    def test_strips_port(self) -> None:
        assert normalize_domain("example.com:8080/x") == "example.com"

    def test_bare_hostname(self) -> None:
        assert normalize_domain("tv.youtube.com") == "tv.youtube.com"

    @pytest.mark.parametrize("value", ["", "   ", "not_a_domain", "http://"])
    def test_invalid_inputs_return_none(self, value: str) -> None:
        assert normalize_domain(value) is None


class TestStripMarkdownJson:
    def test_no_fence(self) -> None:
        assert strip_markdown_json('{"a": 1}') == '{"a": 1}'

    def test_json_fence(self) -> None:
        text = '```json\n{"a": 1}\n```'
        assert strip_markdown_json(text) == '{"a": 1}'

    def test_bare_fence(self) -> None:
        text = '```\n{"a": 1}\n```'
        assert strip_markdown_json(text) == '{"a": 1}'


class TestParseJsonObject:
    def test_strict_dict(self) -> None:
        assert parse_json_object('{"a": 1}') == {"a": 1}

    def test_with_fence(self) -> None:
        assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_with_prose(self) -> None:
        text = 'Here you go:\n{"a": 1}\nThanks!'
        assert parse_json_object(text) == {"a": 1}

    def test_rejects_array(self) -> None:
        with pytest.raises(ValueError):
            parse_json_object("[1, 2, 3]")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            parse_json_object("not json at all")


class TestDedupeKeepOrder:
    def test_preserves_first_seen_order(self) -> None:
        assert dedupe_keep_order(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]

    def test_empty(self) -> None:
        assert dedupe_keep_order([]) == []


class TestChunked:
    def test_even_chunks(self) -> None:
        assert list(chunked([1, 2, 3, 4], 2)) == [[1, 2], [3, 4]]

    def test_uneven_chunk(self) -> None:
        assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]

    def test_zero_size_raises(self) -> None:
        with pytest.raises(ValueError):
            list(chunked([1], 0))


class TestDomainMatches:
    @pytest.mark.parametrize(
        "candidate,target,expected",
        [
            ("youtube.com", "youtube.com", True),
            ("tv.youtube.com", "youtube.com", True),
            ("youtube.com", "tv.youtube.com", True),
            ("example.com", "youtube.com", False),
            ("", "youtube.com", False),
            ("youtube.com", "", False),
        ],
    )
    def test_match_rules(self, candidate: str, target: str, expected: bool) -> None:
        assert domain_matches(candidate, target) is expected
