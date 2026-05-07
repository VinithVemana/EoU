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

Adaptive Playwright fallback
----------------------------
When ``adaptive_playwright_fallback=True`` (default ON when running with
the requests backend), the fetcher tracks the empty-body rate per host
across the run. If a host yields ``adaptive_failure_threshold`` (default
4) consecutive empty bodies and at least ``adaptive_min_observations``
(default 5) observations, the host is marked as **bot-blocked** and all
subsequent fetches for that host are routed through Playwright Chromium
automatically. No CLI flag flip required. Requires Playwright to be
installed; if missing, the fetcher logs a warning and continues serving
empty bodies for the blocked host.

Index-page link harvest
-----------------------
:meth:`PageFetcher.harvest_links` exposes the raw HTML's same-domain
anchor hrefs that sit under the index page's path. Catalogue / overview
pages routinely list dozens of sibling sub-pages inline (e.g. the
``/maps/documentation/mobility/`` index lists every Fleet Engine and
Driver SDK sub-page). Surfacing those links as additional candidate URLs
closes the niche-surface retrieval gap that SerpApi alone cannot.
"""

from __future__ import annotations

import html as html_module
import logging
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

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
    """Thread-pinned Playwright Chromium backend.

    Playwright's sync API binds to the greenlet of the thread that called
    ``sync_playwright().__enter__()`` and refuses to be driven from any
    other thread (``greenlet.error: Cannot switch to a different thread``).
    Locking is not enough — the *thread identity* must match.

    All Playwright operations are therefore funnelled through a single
    dedicated worker thread (``ThreadPoolExecutor(max_workers=1)``).
    Calls submitted from page-fetch worker threads block on
    ``future.result()`` until the worker thread finishes the navigation.
    """

    def __init__(self, *, headless: bool = True, timeout_ms: int = 20_000) -> None:
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._pw = None   # playwright context manager
        self._browser = None
        # max_workers=1 → all jobs run on same OS thread, satisfying the
        # greenlet binding requirement.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pw-fetch"
        )

    def _ensure_started(self) -> None:
        """Init Playwright. MUST run inside the executor thread."""
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

    def _start_in_thread(self) -> None:
        """Public-ish hook used by adaptive init to warm the browser."""
        self._executor.submit(self._ensure_started).result()

    def _fetch_inthread(self, url: str, max_chars: int) -> tuple[str, str]:
        """Body of fetch() that runs on the pinned executor thread."""
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
            try:
                raw_html = page.content()
            except Exception:
                raw_html = ""
            return text[:max_chars], raw_html
        except Exception as exc:
            LOG.debug("Playwright fetch failed url=%s error=%s", url, exc)
            return "", ""
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass

    def fetch(self, url: str, max_chars: int) -> tuple[str, str]:
        """Return (stripped_text, raw_html). raw_html is the full DOM.

        Thread-safe: dispatches to the pinned executor thread.
        """
        try:
            future = self._executor.submit(self._fetch_inthread, url, max_chars)
            return future.result()
        except RuntimeError as exc:
            # Executor already shut down (close() raced with a fetch).
            LOG.debug("Playwright executor unavailable url=%s error=%s", url, exc)
            return "", ""

    def _close_inthread(self) -> None:
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

    def close(self) -> None:
        try:
            self._executor.submit(self._close_inthread).result(timeout=10)
        except Exception:
            pass
        self._executor.shutdown(wait=True)


class _FirecrawlBackend:
    """Thread-safe Firecrawl scrape backend.

    Falls between the requests-based fetcher and Playwright. Used when the
    plain ``requests`` path returns empty (or fails) so bot-blocked pages
    can be rescued without spinning up a local Chromium. Cheaper and faster
    than Playwright; honours Firecrawl's own caching via ``max_age``.
    """

    # 48h cache window — Firecrawl returns cached body if scraped within window.
    _MAX_AGE_MS = 172_800_000

    def __init__(self, *, api_key: str, timeout_ms: int = 30_000) -> None:
        self._api_key = api_key
        self._timeout_ms = int(timeout_ms)
        self._lock = threading.Lock()
        self._app = None

    def _ensure_started(self) -> None:
        if self._app is not None:
            return
        try:
            from firecrawl import Firecrawl  # type: ignore[import]
        except ImportError as exc:
            raise ConfigError(
                "firecrawl-py is required for firecrawl fallback: "
                "pip install firecrawl-py"
            ) from exc
        self._app = Firecrawl(api_key=self._api_key)
        LOG.info("Firecrawl backend initialised")

    def fetch(self, url: str, max_chars: int) -> tuple[str, str]:
        """Return (body_text, raw_html). Both empty on failure."""
        with self._lock:
            self._ensure_started()
            app = self._app
        assert app is not None
        try:
            doc = app.scrape(
                url,
                only_main_content=True,
                max_age=self._MAX_AGE_MS,
                parsers=["pdf"],
                formats=["markdown", "html"],
                timeout=self._timeout_ms,
            )
        except Exception as exc:
            LOG.debug("Firecrawl fetch failed url=%s error=%s", url, exc)
            return "", ""

        markdown = getattr(doc, "markdown", "") or ""
        raw_html = getattr(doc, "html", "") or ""
        # Markdown is already clean text — collapse whitespace + truncate.
        text = _WS_RE.sub(" ", markdown).strip()[:max_chars]
        if not text and raw_html:
            # Markdown unavailable but HTML present — fall back to stripping.
            text = _strip_html(raw_html)[:max_chars]
        return text, raw_html

    def close(self) -> None:
        # Firecrawl SDK is HTTP-based; nothing persistent to release.
        self._app = None


_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>",
    re.IGNORECASE,
)


def _strip_html(html_text: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html_text)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


@dataclass(slots=True)
class _HostStats:
    """Per-host body-fetch outcomes used by the adaptive Playwright switch."""

    observations: int = 0
    empties: int = 0
    consecutive_empties: int = 0
    blocked: bool = False


@dataclass(slots=True)
class _FetchEntry:
    """Cached fetch outcome — body plus raw HTML for link harvest."""

    body: str = ""
    raw_html: str = ""


class PageFetcher:
    """Fetch URLs in parallel, strip HTML, return plain text.

    A single :class:`requests.Session` is reused across calls (connection
    pooling + per-host keepalive). Failures are swallowed and recorded as
    empty bodies so the caller can fall back to the SerpApi snippet.

    When ``use_playwright=True``, :class:`_PlaywrightBackend` is used instead
    of requests. Playwright executes JavaScript and renders the full DOM,
    bypassing simple bot-detection. Requires ``pip install playwright`` and
    ``playwright install chromium``.

    When ``adaptive_playwright_fallback=True`` (default ON unless the
    fetcher is already running on Playwright), per-host empty-body
    statistics are tracked. Once a host crosses the
    ``adaptive_failure_threshold`` consecutive-empty mark with at least
    ``adaptive_min_observations`` total observations, that host is
    promoted to Playwright for the remainder of the run. This recovers
    pages on bot-protected hosts (e.g. ``support.google.com``) without a
    CLI flag flip.
    """

    def __init__(
        self,
        *,
        max_chars: int = 6000,
        timeout: float = 10.0,
        sleep_seconds: float = 0.0,
        user_agent: Optional[str] = None,
        max_workers: int = 8,
        disk_cache: Optional[DiskCache] = None,
        use_playwright: bool = False,
        playwright_headless: bool = True,
        adaptive_playwright_fallback: bool = True,
        adaptive_failure_threshold: int = 4,
        adaptive_min_observations: int = 5,
        keep_raw_html: bool = True,
        firecrawl_api_key: Optional[str] = None,
    ) -> None:
        self.max_chars = max_chars
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.user_agent = user_agent or USER_AGENT
        self.max_workers = max(1, int(max_workers))
        self.keep_raw_html = bool(keep_raw_html)

        self._playwright: Optional[_PlaywrightBackend] = None
        if use_playwright:
            self._playwright = _PlaywrightBackend(
                headless=playwright_headless,
                timeout_ms=int(timeout * 1000),
            )
            self._session = None
            # Already on Playwright — adaptive fallback would be a no-op.
            self._adaptive_enabled = False
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
            self._adaptive_enabled = bool(adaptive_playwright_fallback)

        self._adaptive_threshold = max(1, int(adaptive_failure_threshold))
        self._adaptive_min_obs = max(1, int(adaptive_min_observations))
        self._playwright_headless = playwright_headless
        self._adaptive_backend: Optional[_PlaywrightBackend] = None

        self._firecrawl_api_key = (firecrawl_api_key or "").strip() or None
        self._firecrawl_backend: Optional[_FirecrawlBackend] = None
        self._firecrawl_disabled = self._firecrawl_api_key is None

        self._cache: dict[str, _FetchEntry] = {}
        self._cache_lock = threading.Lock()
        self._disk_cache = disk_cache

        self._host_stats: dict[str, _HostStats] = defaultdict(_HostStats)
        self._stats_lock = threading.Lock()

    @property
    def cache(self) -> dict[str, str]:
        """Read-only view of fetched bodies keyed by URL.

        Kept as ``dict[str, str]`` for backwards compatibility with callers
        that only care about the body text. Use :meth:`raw_html_for` to
        access the raw DOM for link harvesting.
        """
        return {url: entry.body for url, entry in self._cache.items()}

    def raw_html_for(self, url: str) -> str:
        """Return raw HTML last fetched for *url* (empty if never fetched)."""
        with self._cache_lock:
            entry = self._cache.get(url)
            return entry.raw_html if entry is not None else ""

    def fetch(self, url: str) -> str:
        """Fetch and strip a single URL, with cache.

        Lookup order: in-memory cache (per run) → disk cache (across runs)
        → live HTTP. Result populates both layers.
        """
        with self._cache_lock:
            cached = self._cache.get(url)
            if cached is not None:
                return cached.body

        if self._disk_cache is not None:
            disk_hit = self._disk_cache.get({"url": url, "max_chars": self.max_chars})
            if isinstance(disk_hit, str):
                with self._cache_lock:
                    # Disk cache stores body only; raw HTML is not persisted to
                    # save disk space and because the in-run link-harvest
                    # consumer is the only one needing it.
                    self._cache[url] = _FetchEntry(body=disk_hit, raw_html="")
                LOG.debug("Page fetch disk cache hit url=%s", url)
                return disk_hit

        body, raw_html = self._fetch_uncached(url)
        if not self.keep_raw_html:
            raw_html = ""

        with self._cache_lock:
            self._cache[url] = _FetchEntry(body=body, raw_html=raw_html)
        if body and self._disk_cache is not None:
            self._disk_cache.set({"url": url, "max_chars": self.max_chars}, body)

        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        return body

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

    def ensure_raw_html(self, url: str) -> str:
        """Return raw HTML for *url*, fetching it live if not in memory.

        Disk-cached bodies do not carry raw HTML (we only persist the
        stripped text), so a cache hit on ``/mobility/`` may return a
        body but leave ``raw_html`` empty — which is exactly when the
        index-link harvester needs it. This helper re-fetches the page
        on demand to populate the in-memory raw HTML, without disturbing
        the cached body.
        """
        with self._cache_lock:
            entry = self._cache.get(url)
            if entry is not None and entry.raw_html:
                return entry.raw_html

        # Not cached or no HTML — fetch live just for the HTML. This
        # mirrors :meth:`_fetch_uncached` but does not record stats or
        # write to the disk cache (we only need the HTML for harvesting).
        body = ""
        raw_html = ""
        if self._playwright is not None:
            body, raw_html = self._playwright.fetch(url, self.max_chars)
        elif self._session is not None:
            try:
                response = self._session.get(
                    url, timeout=self.timeout, allow_redirects=True
                )
                response.raise_for_status()
                raw_html = response.text
                body = _strip_html(raw_html)[: self.max_chars]
            except Exception as exc:
                LOG.debug(
                    "ensure_raw_html fetch failed url=%s error=%s", url, exc
                )

        if not raw_html:
            fc = self._try_firecrawl(url)
            if fc is not None and fc[1]:
                body, raw_html = fc

        if not raw_html:
            return ""

        with self._cache_lock:
            existing = self._cache.get(url)
            kept_body = existing.body if existing and existing.body else body
            self._cache[url] = _FetchEntry(
                body=kept_body,
                raw_html=raw_html if self.keep_raw_html else "",
            )
        return raw_html

    def harvest_links(
        self,
        url: str,
        *,
        max_links: int = 200,
        prefix_only: bool = True,
    ) -> list[str]:
        """Return same-domain anchor hrefs from the cached raw HTML for *url*.

        Catalogue / overview / index pages routinely list dozens of sibling
        sub-pages inline. Surfacing those links as additional candidate
        URLs closes the niche-surface retrieval gap that SerpApi alone
        cannot close.

        Args:
            url:           Page whose raw HTML should be parsed. Fetched
                           live if not already in memory (because disk
                           cache only persists stripped body text).
            max_links:     Cap on returned URLs.
            prefix_only:   When True (default), only return links whose path
                           starts with the parent URL's path. Restricts the
                           harvest to genuine descendants of an index page.

        Returns:
            Deduplicated absolute-URL list, in the order they appear in the
            HTML. Anchor fragments and query strings are stripped to keep
            the deduped list tight.
        """
        with self._cache_lock:
            entry = self._cache.get(url)
        raw_html = entry.raw_html if entry is not None else ""
        if not raw_html:
            raw_html = self.ensure_raw_html(url)
        if not raw_html:
            return []

        try:
            base_parsed = urlparse(url)
        except Exception:
            return []
        base_host = base_parsed.netloc
        base_path = base_parsed.path or "/"
        if not base_path.endswith("/"):
            base_path = base_path.rsplit("/", 1)[0] + "/"

        seen: set[str] = set()
        out: list[str] = []
        for href in _ANCHOR_RE.findall(raw_html):
            cleaned = href.strip()
            if not cleaned or cleaned.startswith(("#", "mailto:", "javascript:", "tel:")):
                continue
            try:
                absolute = urljoin(url, cleaned)
                parsed = urlparse(absolute)
            except Exception:
                continue
            if parsed.scheme not in ("http", "https"):
                continue
            if parsed.netloc != base_host:
                continue
            # Strip fragment + query to keep the deduped set tight.
            normalized = parsed._replace(fragment="", query="").geturl()
            if normalized.endswith("/index.html"):
                normalized = normalized[: -len("index.html")]
            if prefix_only and not (parsed.path or "/").startswith(base_path.rstrip("/")):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            out.append(normalized)
            if len(out) >= max_links:
                break
        return out

    def _fetch_uncached(self, url: str) -> tuple[str, str]:
        """Return (stripped_body, raw_html) for *url*. Both empty on failure.

        Fallback chain when the requests-based path fails or returns empty:
        1. Firecrawl scrape (if ``FIRECRAWL_API_KEY`` configured) — cheap,
           bypasses bot detection without local browser.
        2. Playwright Chromium (if installed) — full JS render, heaviest.
        """
        if self._playwright is not None:
            return self._playwright.fetch(url, self.max_chars)

        host = self._url_host(url)

        # Adaptive fallback: hosts marked as blocked skip the requests path
        # entirely and go straight to Firecrawl, then Playwright.
        if self._adaptive_enabled and host:
            stats = self._host_stats[host]
            if stats.blocked:
                fc = self._try_firecrawl(url)
                if fc is not None and fc[0]:
                    return fc
                backend = self._ensure_adaptive_backend()
                if backend is not None:
                    return backend.fetch(url, self.max_chars)
                # Neither rescue available — fall through to plain requests
                # so the caller still gets a deterministic empty body rather
                # than an exception.

        try:
            assert self._session is not None
            response = self._session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
            raw_html = response.text
            stripped = _strip_html(raw_html)[: self.max_chars]
        except Exception as exc:
            LOG.debug("Page fetch failed url=%s error=%s", url, exc)
            self._record_observation(host, empty=True)
            fc = self._try_firecrawl(url)
            if fc is not None and fc[0]:
                LOG.info("Firecrawl rescued failed requests fetch url=%s", url)
                return fc
            # Firecrawl unavailable / also empty — try Playwright if this
            # host has crossed the blocked threshold.
            if (
                self._adaptive_enabled
                and host
                and self._host_stats[host].blocked
            ):
                backend = self._ensure_adaptive_backend()
                if backend is not None:
                    return backend.fetch(url, self.max_chars)
            return "", ""

        empty = not stripped
        self._record_observation(host, empty=empty)

        if empty:
            fc = self._try_firecrawl(url)
            if fc is not None and fc[0]:
                LOG.info("Firecrawl rescued empty requests body url=%s", url)
                return fc
            # Adaptive promotion: if THIS host just crossed the blocked
            # threshold inside the requests path, retry through Playwright
            # before giving up. Single retry, no recursion.
            if (
                self._adaptive_enabled
                and host
                and self._host_stats[host].blocked
            ):
                backend = self._ensure_adaptive_backend()
                if backend is not None:
                    LOG.info(
                        "Adaptive Playwright fallback engaged host=%s url=%s",
                        host, url,
                    )
                    return backend.fetch(url, self.max_chars)

        return stripped, raw_html

    def _try_firecrawl(self, url: str) -> Optional[tuple[str, str]]:
        """Attempt a Firecrawl scrape. ``None`` when disabled/unavailable."""
        if self._firecrawl_disabled or not self._firecrawl_api_key:
            return None
        backend = self._ensure_firecrawl_backend()
        if backend is None:
            return None
        return backend.fetch(url, self.max_chars)

    def _ensure_firecrawl_backend(self) -> Optional[_FirecrawlBackend]:
        if self._firecrawl_backend is not None:
            return self._firecrawl_backend
        if not self._firecrawl_api_key:
            return None
        try:
            backend = _FirecrawlBackend(
                api_key=self._firecrawl_api_key,
                timeout_ms=int(self.timeout * 1000),
            )
            backend._ensure_started()
        except ConfigError as exc:
            LOG.warning(
                "Firecrawl fallback wanted but SDK unavailable: %s. "
                "Install with: pip install firecrawl-py",
                exc,
            )
            self._firecrawl_disabled = True
            return None
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("Failed to start Firecrawl backend: %s", exc)
            self._firecrawl_disabled = True
            return None
        self._firecrawl_backend = backend
        return backend

    def _record_observation(self, host: str, *, empty: bool) -> None:
        if not host or not self._adaptive_enabled:
            return
        with self._stats_lock:
            stats = self._host_stats[host]
            stats.observations += 1
            if empty:
                stats.empties += 1
                stats.consecutive_empties += 1
            else:
                stats.consecutive_empties = 0
            if (
                not stats.blocked
                and stats.observations >= self._adaptive_min_obs
                and stats.consecutive_empties >= self._adaptive_threshold
            ):
                stats.blocked = True
                LOG.warning(
                    "Adaptive fetcher: host=%s flagged as bot-blocked "
                    "(observations=%d empties=%d consecutive=%d) — "
                    "routing remaining URLs through Playwright",
                    host, stats.observations, stats.empties,
                    stats.consecutive_empties,
                )

    def _ensure_adaptive_backend(self) -> Optional[_PlaywrightBackend]:
        if self._adaptive_backend is not None:
            return self._adaptive_backend
        try:
            backend = _PlaywrightBackend(
                headless=self._playwright_headless,
                timeout_ms=int(self.timeout * 1000),
            )
            # Eager init so first fetch is hot — runs inside pinned thread.
            backend._start_in_thread()
        except ConfigError as exc:
            LOG.warning(
                "Adaptive fallback wanted Playwright but it is unavailable: %s. "
                "Install with: pip install playwright && playwright install chromium",
                exc,
            )
            # Disable further attempts so we don't spam the log.
            self._adaptive_enabled = False
            return None
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("Failed to start adaptive Playwright backend: %s", exc)
            self._adaptive_enabled = False
            return None
        self._adaptive_backend = backend
        return backend

    @staticmethod
    def _url_host(url: str) -> str:
        try:
            return urlparse(url).netloc.lower()
        except Exception:
            return ""

    def host_stats_snapshot(self) -> dict[str, dict[str, int | bool]]:
        """Return a copy of per-host observation stats — primarily for tracing."""
        with self._stats_lock:
            return {
                host: {
                    "observations": s.observations,
                    "empties": s.empties,
                    "consecutive_empties": s.consecutive_empties,
                    "blocked": s.blocked,
                }
                for host, s in self._host_stats.items()
            }

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
        if self._playwright is not None:
            self._playwright.close()
        if self._adaptive_backend is not None:
            self._adaptive_backend.close()
        if self._firecrawl_backend is not None:
            self._firecrawl_backend.close()

    def __enter__(self) -> "PageFetcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["PageFetcher"]
