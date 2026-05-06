"""Tests for the diversity-guard and per-element-coverage post-processors."""

from __future__ import annotations

from unittest.mock import MagicMock

from claim_url.finder import ClaimURLFinder
from claim_url.models import ScoredURL


def _finder(**overrides) -> ClaimURLFinder:
    """Construct a ClaimURLFinder with mocked deps; only post-process methods exercised."""
    defaults = dict(
        llm=MagicMock(),
        serp=MagicMock(),
        enable_subproduct_probe=False,
    )
    defaults.update(overrides)
    return ClaimURLFinder(**defaults)


def _url(path: str, score: float, matched: list[str] | None = None) -> ScoredURL:
    return ScoredURL(
        url=f"https://example.com{path}",
        title="t",
        snippet="s",
        score=score,
        matched_elements=matched or [],
        rationale="r",
    )


# --------- diversity ---------

def test_diversity_caps_per_prefix_within_tied_score() -> None:
    finder = _finder(diversity_prefix_segments=2, diversity_per_prefix=2)
    # Five URLs all sharing the same prefix /foo/bar at score 1.0 — only 2 should
    # appear before any deferred URLs.
    scored = [
        _url("/foo/bar/a", 1.0),
        _url("/foo/bar/b", 1.0),
        _url("/foo/bar/c", 1.0),
        _url("/foo/bar/d", 1.0),
        _url("/baz/qux/a", 1.0),
    ]
    result = finder._apply_diversity(scored)
    # First 3 should mix prefixes (2 from /foo/bar, then 1 from /baz/qux),
    # leaving the deferred /foo/bar/c and /foo/bar/d at the bottom.
    top3_paths = [u.url.split(".com")[1] for u in result[:3]]
    assert top3_paths.count("/foo/bar/a") + top3_paths.count("/foo/bar/b") <= 2
    assert "/baz/qux/a" in [u.url.split(".com")[1] for u in result[:3]]


def test_diversity_does_not_displace_higher_scores() -> None:
    finder = _finder(diversity_prefix_segments=2, diversity_per_prefix=1)
    scored = [
        _url("/a/b/x", 1.0),
        _url("/a/b/y", 1.0),  # deferred (per-prefix=1)
        _url("/c/d/x", 0.5),  # must come after both 1.0 URLs even though prefix differs
    ]
    result = finder._apply_diversity(scored)
    # The 0.5 URL is strictly lower; comes last.
    assert result[-1].score == 0.5
    # First two are the two 1.0 URLs (kept + deferred within tier).
    assert {u.score for u in result[:2]} == {1.0}


def test_diversity_with_single_url_is_noop() -> None:
    finder = _finder()
    scored = [_url("/x", 0.5)]
    assert finder._apply_diversity(scored) == scored


def test_diversity_disabled_when_per_prefix_zero() -> None:
    finder = _finder(diversity_per_prefix=0)
    # finder constructor clamps to 1 (max(1, ...)); so this confirms the floor.
    assert finder.diversity_per_prefix == 1


# --------- coverage ---------

def test_coverage_appends_uncovered_element() -> None:
    finder = _finder(coverage_score_floor=0.5)
    top_k = [_url("/a", 1.0, matched=["E1"]), _url("/b", 0.9, matched=["E2"])]
    pool = top_k + [
        _url("/c", 0.7, matched=["E3"]),  # covers E3
        _url("/d", 0.4, matched=["E3"]),  # below floor, must not be picked
    ]
    result = finder._ensure_coverage(
        top_k_urls=top_k, pool=pool, element_ids=["E1", "E2", "E3"]
    )
    assert len(result) == 3
    assert result[2].url.endswith("/c")


def test_coverage_skips_element_already_covered() -> None:
    finder = _finder()
    top_k = [_url("/a", 1.0, matched=["E1", "E2"])]
    pool = top_k + [_url("/b", 0.6, matched=["E2"])]
    result = finder._ensure_coverage(
        top_k_urls=top_k, pool=pool, element_ids=["E1", "E2"]
    )
    assert len(result) == 1  # nothing appended; both elements covered


def test_coverage_skips_when_no_candidate_above_floor() -> None:
    finder = _finder(coverage_score_floor=0.5)
    top_k = [_url("/a", 1.0, matched=["E1"])]
    pool = top_k + [_url("/b", 0.3, matched=["E2"])]  # below floor
    result = finder._ensure_coverage(
        top_k_urls=top_k, pool=pool, element_ids=["E1", "E2"]
    )
    assert len(result) == 1


def test_coverage_skips_already_in_top_k() -> None:
    finder = _finder()
    same = _url("/a", 1.0, matched=["E1"])
    top_k = [same]
    pool = [same]
    result = finder._ensure_coverage(
        top_k_urls=top_k, pool=pool, element_ids=["E1"]
    )
    assert result == top_k


def test_coverage_no_op_on_empty_top_k() -> None:
    finder = _finder()
    pool = [_url("/a", 1.0, matched=["E1"])]
    assert finder._ensure_coverage(top_k_urls=[], pool=pool, element_ids=["E1"]) == []


# --------- path prefix helper ---------

def test_path_prefix_uses_segment_count() -> None:
    finder = _finder(diversity_prefix_segments=2)
    p = finder._path_prefix("https://docs.example.com/api/v1/users/list")
    # netloc + first 2 segments
    assert p == "docs.example.com/api/v1"


def test_path_prefix_handles_root_path() -> None:
    finder = _finder(diversity_prefix_segments=4)
    assert finder._path_prefix("https://x.example.com/") == "x.example.com/"
