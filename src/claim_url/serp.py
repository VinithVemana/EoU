"""SerpApi Google Search client."""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Optional

from claim_url.cache import DiskCache
from claim_url.config import ENV_SERPAPI_KEY
from claim_url.errors import ConfigError, SearchError
from claim_url.models import SearchResult


LOG = logging.getLogger("claim-url-finder")

_NO_RESULT_MARKERS = ("hasn't returned any results", "no results")


def _backoff_seconds(attempt: int, *, base: float = 2.0, cap: float = 10.0) -> float:
    raw = min(base * attempt, cap)
    return raw + random.uniform(0.0, 0.25 * raw)


class SerpApiClient:
    """Thin SerpApi wrapper with bounded retries and a "no results" short-circuit.

    Optionally fronted by a :class:`~claim_url.cache.DiskCache`. When the
    cache is set, identical (engine, gl, hl, query, num) tuples skip the
    network entirely — same-run dedupe in
    :class:`~claim_url.agents.search.OfficialDomainSearch` already
    collapses repeats inside one run; the disk cache extends that across
    runs (and across Agent 1 probe queries that recur for a given
    product).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        engine: str = "google",
        gl: str = "us",
        hl: str = "en",
        cache: Optional[DiskCache] = None,
    ) -> None:
        api_key = api_key or os.getenv(ENV_SERPAPI_KEY)
        if not api_key:
            raise ConfigError(f"{ENV_SERPAPI_KEY} is required")

        try:
            from serpapi import GoogleSearch
        except ImportError as exc:
            raise ConfigError(
                "serpapi SDK not installed: pip install google-search-results"
            ) from exc

        self._api_key = api_key
        self.engine = engine
        self.gl = gl
        self.hl = hl
        self._google_search_cls = GoogleSearch
        self._cache = cache

    def _cache_key(self, query: str, num: int) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "gl": self.gl,
            "hl": self.hl,
            "q": query,
            "num": num,
        }

    def search(
        self,
        query: str,
        *,
        num: int = 10,
        retries: int = 3,
    ) -> list[SearchResult]:
        """Run a single SerpApi query.

        Returns an empty list when SerpApi reports "no results" — narrow
        ``site:`` queries hit this case routinely and it is not a failure.
        Raises :class:`~claim_url.errors.SearchError` if every retry fails.
        """
        if retries < 1:
            raise ValueError("retries must be >= 1")

        key = self._cache_key(query, num)
        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                LOG.debug("SerpApi cache hit query=%r", query)
                return [SearchResult(**item) for item in cached]

        last_error: Optional[BaseException] = None

        for attempt in range(1, retries + 1):
            try:
                params: dict[str, Any] = {
                    "engine": self.engine,
                    "q": query,
                    "api_key": self._api_key,
                    "num": num,
                    "gl": self.gl,
                    "hl": self.hl,
                }
                LOG.debug("SerpApi query=%r", query)

                data = self._google_search_cls(params).get_dict()

                if "error" in data:
                    error_text = str(data["error"]).lower()
                    if any(marker in error_text for marker in _NO_RESULT_MARKERS):
                        LOG.debug("SerpApi no-results query=%r", query)
                        results: list[SearchResult] = []
                        if self._cache is not None:
                            self._cache.set(key, [])
                        return results
                    raise RuntimeError(data["error"])

                results = self._parse_organic(data)
                if self._cache is not None:
                    self._cache.set(
                        key,
                        [
                            {"url": r.url, "title": r.title, "snippet": r.snippet}
                            for r in results
                        ],
                    )
                return results

            except Exception as exc:
                last_error = exc
                LOG.warning(
                    "SerpApi search failed attempt=%d/%d query=%r error=%s",
                    attempt,
                    retries,
                    query,
                    exc,
                )
                if attempt == retries:
                    break
                time.sleep(_backoff_seconds(attempt))

        raise SearchError(f"SerpApi search failed after {retries} attempts") from last_error

    @staticmethod
    def _parse_organic(data: dict[str, Any]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for item in data.get("organic_results", []) or []:
            url = item.get("link") or item.get("url")
            if not url:
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=item.get("title") or "",
                    snippet=item.get("snippet") or item.get("content") or "",
                )
            )
        return results


__all__ = ["SerpApiClient"]
