"""HTML page fetcher used to enrich Agent 2's relevance scoring.

SerpApi snippets are short SEO blurbs and routinely understate page
relevance. Fetching the page body and handing the first ~4000 stripped
chars to the relevance agent significantly improves precision (mirrors
what websearch tools do internally).
"""

from __future__ import annotations

import html as html_module
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Optional

from claim_url.config import USER_AGENT
from claim_url.errors import ConfigError


LOG = logging.getLogger("claim-url-finder")


_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html_text: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html_text)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


class PageFetcher:
    """Fetch URLs in parallel, strip HTML, return plain text.

    A single :class:`requests.Session` is reused across calls (connection
    pooling + per-host keepalive). Failures are swallowed and recorded as
    empty bodies so the caller can fall back to the SerpApi snippet.
    """

    def __init__(
        self,
        *,
        max_chars: int = 4000,
        timeout: float = 10.0,
        sleep_seconds: float = 0.0,
        user_agent: Optional[str] = None,
        max_workers: int = 8,
    ) -> None:
        self.max_chars = max_chars
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.user_agent = user_agent or USER_AGENT
        self.max_workers = max(1, int(max_workers))

        try:
            import requests
        except ImportError as exc:
            raise ConfigError(
                "requests is required for --fetch-pages: pip install requests"
            ) from exc

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})

        self._cache: dict[str, str] = {}
        self._cache_lock = threading.Lock()

    @property
    def cache(self) -> dict[str, str]:
        """Read-only view of fetched bodies keyed by URL."""
        return self._cache

    def fetch(self, url: str) -> str:
        """Fetch and strip a single URL, with cache."""
        with self._cache_lock:
            cached = self._cache.get(url)
            if cached is not None:
                return cached

        text = self._fetch_uncached(url)

        with self._cache_lock:
            self._cache[url] = text

        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        return text

    def fetch_many(self, urls: Iterable[str]) -> dict[str, str]:
        """Fetch many URLs in parallel using a bounded thread pool."""
        unique = [u for u in dict.fromkeys(urls) if u]
        if not unique:
            return {}

        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self.fetch, url): url for url in unique}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    results[url] = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    LOG.debug("Page fetch worker error url=%s error=%s", url, exc)
                    results[url] = ""
        return results

    def _fetch_uncached(self, url: str) -> str:
        try:
            response = self._session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
            return _strip_html(response.text)[: self.max_chars]
        except Exception as exc:
            LOG.debug("Page fetch failed url=%s error=%s", url, exc)
            return ""

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "PageFetcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["PageFetcher"]
