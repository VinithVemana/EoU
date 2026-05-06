"""HTML page fetcher used to enrich Agent 2's relevance scoring.

SerpApi snippets are short SEO blurbs and routinely understate page
relevance. Fetching the page body and handing the first ~4000 stripped
chars to the relevance agent significantly improves precision (mirrors
what websearch tools do internally).

When ``use_playwright=True`` (CLI: ``--playwright-fetch``), Playwright
Chromium is used instead of plain requests. Playwright executes JavaScript
and renders the full DOM before extracting text, bypassing simple bot-
detection mechanisms that block the requests-based fetcher (e.g. Google
support pages). Requires ``pip install playwright`` and
``playwright install chromium``.
"""

from __future__ import annotations

import html as html_module
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Optional

from claim_url.cache import DiskCache
from claim_url.config import USER_AGENT
from claim_url.errors import ConfigError


LOG = logging.getLogger("claim-url-finder")

_PLAYWRIGHT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class _PlaywrightBackend:
    """Thread-safe Playwright Chromium backend.

    One browser process is shared; each ``fetch`` call opens a new page,
    navigates, extracts ``body`` text, then closes the page. The browser
    is launched lazily on the first fetch call and closed via ``close()``.
    """

    def __init__(self, *, headless: bool = True, timeout_ms: int = 20_000) -> None:
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._lock = threading.Lock()
        self._pw = None   # playwright context manager
        self._browser = None

    def _ensure_started(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import]
        except ImportError as exc:
            raise ConfigError(
                "playwright is required for --playwright-fetch: "
                "pip install playwright && playwright install chromium"
            ) from exc
        self._pw = sync_playwright().__enter__()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        LOG.info("Playwright Chromium launched (headless=%s)", self._headless)

    def fetch(self, url: str, max_chars: int) -> str:
        with self._lock:
            self._ensure_started()
            assert self._browser is not None
            ctx = self._browser.new_context(
                user_agent=_PLAYWRIGHT_UA,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            text = page.inner_text("body")
            text = re.sub(r"\s+", " ", text).strip()
            return text[:max_chars]
        except Exception as exc:
            LOG.debug("Playwright fetch failed url=%s error=%s", url, exc)
            return ""
        finally:
            page.close()
            ctx.close()

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.__exit__(None, None, None)
            except Exception:
                pass
        self._browser = None
        self._pw = None


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

    When ``use_playwright=True``, :class:`_PlaywrightBackend` is used instead
    of requests. Playwright executes JavaScript and renders the full DOM,
    bypassing simple bot-detection. Requires ``pip install playwright`` and
    ``playwright install chromium``.
    """

    def __init__(
        self,
        *,
        max_chars: int = 4000,
        timeout: float = 10.0,
        sleep_seconds: float = 0.0,
        user_agent: Optional[str] = None,
        max_workers: int = 8,
        disk_cache: Optional[DiskCache] = None,
        use_playwright: bool = False,
        playwright_headless: bool = True,
    ) -> None:
        self.max_chars = max_chars
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.user_agent = user_agent or USER_AGENT
        self.max_workers = max(1, int(max_workers))

        self._playwright: Optional[_PlaywrightBackend] = None
        if use_playwright:
            self._playwright = _PlaywrightBackend(
                headless=playwright_headless,
                timeout_ms=int(timeout * 1000),
            )
            self._session = None
        else:
            try:
                import requests
            except ImportError as exc:
                raise ConfigError(
                    "requests is required for --fetch-pages: pip install requests"
                ) from exc

            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
            })

        self._cache: dict[str, str] = {}
        self._cache_lock = threading.Lock()
        self._disk_cache = disk_cache

    @property
    def cache(self) -> dict[str, str]:
        """Read-only view of fetched bodies keyed by URL."""
        return self._cache

    def fetch(self, url: str) -> str:
        """Fetch and strip a single URL, with cache.

        Lookup order: in-memory cache (per run) → disk cache (across runs)
        → live HTTP. Result populates both layers.
        """
        with self._cache_lock:
            cached = self._cache.get(url)
            if cached is not None:
                return cached

        if self._disk_cache is not None:
            disk_hit = self._disk_cache.get({"url": url, "max_chars": self.max_chars})
            if isinstance(disk_hit, str):
                with self._cache_lock:
                    self._cache[url] = disk_hit
                LOG.debug("Page fetch disk cache hit url=%s", url)
                return disk_hit

        text = self._fetch_uncached(url)

        with self._cache_lock:
            self._cache[url] = text
        if text and self._disk_cache is not None:
            self._disk_cache.set({"url": url, "max_chars": self.max_chars}, text)

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
        if self._playwright is not None:
            return self._playwright.fetch(url, self.max_chars)
        try:
            assert self._session is not None
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
        if self._session is not None:
            self._session.close()
        if self._playwright is not None:
            self._playwright.close()

    def __enter__(self) -> "PageFetcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["PageFetcher"]
