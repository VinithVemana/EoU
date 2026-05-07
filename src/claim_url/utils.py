"""Pure utility helpers shared across the package."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Iterator, Optional, TypeVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


T = TypeVar("T")


_FENCE_OPEN_RE = re.compile(r"^```(?:json)?", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"```$")
_DOMAIN_VALIDATION_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


def strip_markdown_json(text: str) -> str:
    """Strip the surrounding ``` fences (with or without ``json``)."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    text = _FENCE_OPEN_RE.sub("", text).strip()
    text = _FENCE_CLOSE_RE.sub("", text).strip()
    return text


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object out of an LLM response.

    Tries strict parse first, then falls back to extracting the outermost
    ``{ ... }`` slice. Always returns a ``dict``.
    """
    cleaned = strip_markdown_json(text)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        return data

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Could not find JSON object in response: {text[:500]!r}")

    candidate = cleaned[start : end + 1]
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object in response")
    return data


def normalize_domain(value: str) -> Optional[str]:
    """Normalize a URL or bare hostname into a lowercase, port-free domain.

    Returns ``None`` if the value cannot be coerced to a plausible domain.

    Examples
    --------
    >>> normalize_domain("https://support.google.com/youtube")
    'support.google.com'
    >>> normalize_domain("www.youtube.com")
    'youtube.com'
    """
    if not value:
        return None

    value = value.strip().lower()
    if not value:
        return None

    target = value if "://" in value else f"https://{value}"
    parsed = urlparse(target)
    domain = parsed.netloc or parsed.path.split("/")[0]
    domain = domain.strip().lower().strip(".").split(":")[0]

    if domain.startswith("www."):
        domain = domain[4:]

    if not _DOMAIN_VALIDATION_RE.match(domain):
        return None
    return domain


def chunked(items: list[T], size: int) -> Iterator[list[T]]:
    """Yield contiguous chunks of ``size`` items from ``items``."""
    if size <= 0:
        raise ValueError("chunk size must be > 0")
    for i in range(0, len(items), size):
        yield items[i : i + size]


def dedupe_keep_order(items: Iterable[T]) -> list[T]:
    """Return ``items`` with duplicates removed, preserving first-seen order."""
    seen: set[T] = set()
    out: list[T] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


# Query params dropped by canonicalize_url. Locale/region knobs almost never
# change page substance — the same support article served as ?hl=en and
# ?hl=en-GB is one URL for our purposes. utm_* and friends are pure
# tracking. gclid/fbclid/msclkid are click identifiers.
_LOCALE_QUERY_PARAMS: frozenset[str] = frozenset({
    "hl", "gl", "lr", "lang", "language", "locale", "ui", "uloc",
    "country", "region", "geo",
})

_TRACKING_QUERY_PARAMS: frozenset[str] = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer",
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
    "ref", "ref_src", "ref_url", "src", "source",
    "_hsenc", "_hsmi", "hsCtaTracking",
    "yclid", "dclid", "igshid", "spm",
})

_DROP_QUERY_PARAMS: frozenset[str] = _LOCALE_QUERY_PARAMS | _TRACKING_QUERY_PARAMS


def canonicalize_url(url: str) -> str:
    """Return a stable canonical form of ``url`` for dedupe / cache keys.

    Collapses cosmetic variants that point at the same content: locale
    knobs (``hl``, ``gl``, ``lang``, ``locale``, …), marketing trackers
    (``utm_*``, ``gclid``, ``fbclid``, …), URL fragments, trailing
    slashes, lowercase scheme/host. Path casing is preserved (case-
    sensitive on most servers).

    Returns the input unchanged when parsing fails so callers can pass
    arbitrary strings without try/except.

    Examples
    --------
    >>> canonicalize_url("https://support.google.com/youtube/answer/6342839?hl=en")
    'https://support.google.com/youtube/answer/6342839'
    >>> canonicalize_url("https://support.google.com/youtube/answer/6342839?hl=en-GB")
    'https://support.google.com/youtube/answer/6342839'
    >>> canonicalize_url("https://Example.com/Docs/?utm_source=x#frag")
    'https://example.com/Docs'
    """
    if not url or "://" not in url:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return url

    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()

    path = parsed.path or "/"
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _DROP_QUERY_PARAMS
    ]
    query = urlencode(query_pairs, doseq=True)

    return urlunparse((scheme, host, path, parsed.params, query, ""))


def domain_matches(url_domain: str, target: str) -> bool:
    """True iff ``url_domain`` equals, is a subdomain of, or is parent of ``target``.

    Mirrors the acceptance rule used by :class:`~claim_url.agents.search.OfficialDomainSearch`.
    """
    if not url_domain or not target:
        return False
    return (
        url_domain == target
        or url_domain.endswith(f".{target}")
        or target.endswith(f".{url_domain}")
    )


# Multi-tenant hosts where every URL is owned by a different tenant. A bare
# ``site:github.com`` matches every repo on the planet, so the domain agent
# (and --domains override) must emit these with a vendor path attached, e.g.
# ``github.com/Netflix``. Used by parse_domain_spec to validate path-less
# specs and by the domain agent prompt to enumerate examples.
MULTI_TENANT_HOSTS: frozenset[str] = frozenset({
    "github.com", "gitlab.com", "bitbucket.org", "sourceforge.net",
    "medium.com", "dev.to", "substack.com", "wordpress.com", "blogspot.com",
    "youtube.com", "vimeo.com", "twitch.tv",
    "twitter.com", "x.com", "facebook.com", "linkedin.com", "instagram.com",
    "tiktok.com", "reddit.com", "pinterest.com",
    "npmjs.com", "pypi.org", "rubygems.org", "crates.io", "packagist.org",
    "stackoverflow.com", "stackexchange.com", "quora.com",
    "docker.com", "hub.docker.com",
    "notion.site", "gitbook.io", "readthedocs.io", "readthedocs.org",
    "hashnode.dev", "hashnode.com",
})


def is_multi_tenant_host(host: str) -> bool:
    if not host:
        return False
    h = host.lower().lstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return h in MULTI_TENANT_HOSTS


def _normalize_path_prefix(raw: str) -> str:
    """Coerce a path slice like ``/Netflix/`` or ``Netflix`` into ``/Netflix``.

    Empty / root-only inputs collapse to ``""``.
    """
    if not raw:
        return ""
    p = raw.strip()
    if not p or p == "/":
        return ""
    if not p.startswith("/"):
        p = "/" + p
    while p.endswith("/") and len(p) > 1:
        p = p[:-1]
    return p


def parse_domain_spec(value: str):
    """Parse ``"host"`` or ``"host/path"`` into a :class:`DomainSpec`.

    Returns ``None`` when ``value`` doesn't look like a valid domain. The
    host is normalized via :func:`normalize_domain`; the path slice is kept
    case-preserved (paths are case-sensitive on most servers) and stripped of
    trailing slashes.

    Imported lazily inside the function to avoid a circular import with
    :mod:`claim_url.models`.

    Examples
    --------
    >>> spec = parse_domain_spec("github.com/Netflix")
    >>> spec.host, spec.path_prefix
    ('github.com', '/Netflix')
    >>> parse_domain_spec("https://github.com/Netflix/zuul").path_prefix
    '/Netflix/zuul'
    >>> parse_domain_spec("support.google.com").path_prefix
    ''
    """
    from claim_url.models import DomainSpec

    if not value:
        return None
    text = value.strip()
    if not text:
        return None

    if "://" in text:
        parsed = urlparse(text)
        host = normalize_domain(parsed.netloc)
        path = _normalize_path_prefix(parsed.path or "")
    else:
        head, _, tail = text.partition("/")
        host = normalize_domain(head)
        path = _normalize_path_prefix(tail)

    if not host:
        return None
    return DomainSpec(host=host, path_prefix=path)


def url_matches_spec(url: str, spec) -> bool:
    """True iff *url* belongs to *spec*'s host AND path starts with the prefix.

    Path comparison is case-insensitive — multi-tenant hosts (GitHub, GitLab)
    accept either case in the URL but redirect to the canonical form, so a
    case-sensitive match would spuriously reject valid URLs.
    """
    if not url or spec is None:
        return False
    url_host = normalize_domain(url) or ""
    if not domain_matches(url_host, spec.host):
        return False
    if not spec.path_prefix:
        return True
    try:
        path = urlparse(url).path or "/"
    except Exception:
        return False
    prefix = spec.path_prefix.rstrip("/")
    if not prefix:
        return True
    p_lower = path.lower()
    pre_lower = prefix.lower()
    return p_lower == pre_lower or p_lower.startswith(pre_lower + "/")


__all__ = [
    "MULTI_TENANT_HOSTS",
    "canonicalize_url",
    "chunked",
    "dedupe_keep_order",
    "domain_matches",
    "is_multi_tenant_host",
    "normalize_domain",
    "parse_domain_spec",
    "parse_json_object",
    "strip_markdown_json",
    "url_matches_spec",
]
