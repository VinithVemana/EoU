from __future__ import annotations

import pytest

from claim_url.utils import (
    canonicalize_url,
    chunked,
    dedupe_keep_order,
    domain_matches,
    is_multi_tenant_host,
    normalize_domain,
    parse_domain_spec,
    parse_json_object,
    strip_markdown_json,
    url_matches_spec,
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


class TestCanonicalizeUrl:
    def test_strips_hl_param(self) -> None:
        a = canonicalize_url("https://support.google.com/youtube/answer/6342839?hl=en")
        b = canonicalize_url("https://support.google.com/youtube/answer/6342839?hl=en-GB")
        assert a == b == "https://support.google.com/youtube/answer/6342839"

    def test_strips_multiple_locale_params(self) -> None:
        url = "https://example.com/docs?hl=en&gl=us&lang=en-US&locale=en"
        assert canonicalize_url(url) == "https://example.com/docs"

    def test_strips_utm_and_click_ids(self) -> None:
        url = "https://example.com/x?utm_source=google&utm_campaign=foo&gclid=abc&fbclid=def"
        assert canonicalize_url(url) == "https://example.com/x"

    def test_drops_fragment(self) -> None:
        assert canonicalize_url("https://example.com/x#section") == "https://example.com/x"

    def test_lowercases_scheme_and_host(self) -> None:
        assert canonicalize_url("HTTPS://Example.COM/Docs") == "https://example.com/Docs"

    def test_preserves_path_case(self) -> None:
        # Many docs sites are case-sensitive on path; do not lowercase path.
        out = canonicalize_url("https://developer.apple.com/documentation/UIKit")
        assert out == "https://developer.apple.com/documentation/UIKit"

    def test_strips_trailing_slash(self) -> None:
        assert canonicalize_url("https://example.com/docs/") == "https://example.com/docs"

    def test_root_slash_preserved(self) -> None:
        assert canonicalize_url("https://example.com/") == "https://example.com/"

    def test_strips_index_html(self) -> None:
        assert canonicalize_url("https://example.com/docs/index.html") == "https://example.com/docs"

    def test_keeps_content_query_params(self) -> None:
        out = canonicalize_url("https://example.com/article?id=42&page=2")
        assert "id=42" in out and "page=2" in out

    def test_passthrough_for_invalid(self) -> None:
        assert canonicalize_url("") == ""
        assert canonicalize_url("not a url") == "not a url"

    def test_idempotent(self) -> None:
        once = canonicalize_url("https://support.google.com/youtube/answer/1?hl=en-GB")
        assert canonicalize_url(once) == once


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


class TestParseDomainSpec:
    def test_bare_host(self) -> None:
        spec = parse_domain_spec("support.google.com")
        assert spec.host == "support.google.com"
        assert spec.path_prefix == ""

    def test_host_with_path(self) -> None:
        spec = parse_domain_spec("github.com/Netflix")
        assert spec.host == "github.com"
        assert spec.path_prefix == "/Netflix"

    def test_full_url_with_deep_path(self) -> None:
        spec = parse_domain_spec("https://github.com/Netflix/zuul")
        assert spec.host == "github.com"
        assert spec.path_prefix == "/Netflix/zuul"

    def test_strips_trailing_slash(self) -> None:
        spec = parse_domain_spec("github.com/Netflix/")
        assert spec.path_prefix == "/Netflix"

    def test_strips_www(self) -> None:
        spec = parse_domain_spec("www.youtube.com/@netflix")
        assert spec.host == "youtube.com"
        assert spec.path_prefix == "/@netflix"

    def test_site_query_renders_host_and_path(self) -> None:
        spec = parse_domain_spec("github.com/Netflix")
        assert spec.site_query() == "github.com/Netflix"

    def test_site_query_for_bare_host(self) -> None:
        spec = parse_domain_spec("example.com")
        assert spec.site_query() == "example.com"

    @pytest.mark.parametrize("value", ["", "   ", "/", "not_a_domain", "//"])
    def test_invalid_inputs_return_none(self, value: str) -> None:
        assert parse_domain_spec(value) is None


class TestIsMultiTenantHost:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("github.com", True),
            ("medium.com", True),
            ("youtube.com", True),
            ("npmjs.com", True),
            ("support.google.com", False),
            ("netflixtechblog.com", False),
            ("", False),
        ],
    )
    def test_known_hosts(self, host: str, expected: bool) -> None:
        assert is_multi_tenant_host(host) is expected


class TestUrlMatchesSpec:
    def test_bare_host_spec_matches_any_path(self) -> None:
        spec = parse_domain_spec("support.google.com")
        assert url_matches_spec("https://support.google.com/youtube/answer/1", spec)
        assert url_matches_spec("https://support.google.com/", spec)

    def test_path_scoped_spec_accepts_vendor_path(self) -> None:
        spec = parse_domain_spec("github.com/Netflix")
        assert url_matches_spec("https://github.com/Netflix/zuul", spec)
        assert url_matches_spec("https://github.com/Netflix/zuul/wiki", spec)
        # Exact prefix match (no trailing path segment) also accepted.
        assert url_matches_spec("https://github.com/Netflix", spec)

    def test_path_scoped_spec_rejects_other_tenants(self) -> None:
        spec = parse_domain_spec("github.com/Netflix")
        assert not url_matches_spec(
            "https://github.com/akash-coded/spring-framework/discussions/164", spec
        )
        assert not url_matches_spec(
            "https://github.com/xinrong-meng/knowledge-sharing", spec
        )

    def test_path_scoped_does_not_accept_substring_match(self) -> None:
        """``/Netflix`` must not match ``/Netflix-foo`` (different tenant)."""
        spec = parse_domain_spec("github.com/Netflix")
        assert not url_matches_spec("https://github.com/Netflix-Skunkworks/x", spec)

    def test_case_insensitive_path_match(self) -> None:
        spec = parse_domain_spec("github.com/Netflix")
        assert url_matches_spec("https://github.com/netflix/zuul", spec)

    def test_subdomain_acceptance_for_bare_host(self) -> None:
        spec = parse_domain_spec("youtube.com")
        assert url_matches_spec("https://tv.youtube.com/guide", spec)
