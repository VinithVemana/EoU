"""Smoke tests for PageFetcher.harvest_links — the niche surface harvest path."""

from __future__ import annotations

from claim_url.fetch import PageFetcher, _FetchEntry


def _make_fetcher() -> PageFetcher:
    pf = PageFetcher()
    return pf


def test_harvest_links_uses_local_raw_html_when_cache_was_empty():
    """Regression: harvest_links must not assume cache entry exists after
    ensure_raw_html. Earlier code referenced ``entry.raw_html`` which was
    None when the URL had not been fetched before, causing AttributeError."""
    pf = _make_fetcher()
    try:
        # No cache entry yet. ensure_raw_html returns the HTML string but does
        # not update the local `entry` reference inside harvest_links.
        pf.ensure_raw_html = lambda url: (
            '<html><body>'
            '<a href="https://example.com/foo">a</a>'
            '<a href="https://example.com/bar">b</a>'
            '</body></html>'
        )
        links = pf.harvest_links(
            "https://example.com/test", max_links=10, prefix_only=False
        )
        assert links == [
            "https://example.com/foo",
            "https://example.com/bar",
        ]
    finally:
        pf.close()


def test_harvest_links_returns_empty_when_no_html_available():
    pf = _make_fetcher()
    try:
        pf.ensure_raw_html = lambda url: ""
        assert pf.harvest_links("https://example.com/missing") == []
    finally:
        pf.close()


def test_harvest_links_uses_cached_raw_html_when_present():
    pf = _make_fetcher()
    try:
        pf._cache["https://example.com/cached"] = _FetchEntry(
            body="ignored",
            raw_html='<a href="https://example.com/cached/child">c</a>',
        )
        # ensure_raw_html should not be invoked when cache already has HTML.
        pf.ensure_raw_html = lambda url: (_ for _ in ()).throw(
            AssertionError("ensure_raw_html should not be called for cached entry")
        )
        links = pf.harvest_links(
            "https://example.com/cached", max_links=10, prefix_only=False
        )
        assert links == ["https://example.com/cached/child"]
    finally:
        pf.close()


def test_harvest_links_prefix_only_drops_off_path_anchors():
    pf = _make_fetcher()
    try:
        pf._cache["https://example.com/docs/index"] = _FetchEntry(
            body="",
            raw_html=(
                '<a href="https://example.com/docs/page-a">a</a>'
                '<a href="https://example.com/blog/post">b</a>'
                '<a href="https://example.com/docs/sub/page-c">c</a>'
            ),
        )
        links = pf.harvest_links(
            "https://example.com/docs/index", max_links=10, prefix_only=True
        )
        assert "https://example.com/docs/page-a" in links
        assert "https://example.com/docs/sub/page-c" in links
        assert "https://example.com/blog/post" not in links
    finally:
        pf.close()
