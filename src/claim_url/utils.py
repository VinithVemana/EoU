"""Pure utility helpers shared across the package."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Iterator, Optional, TypeVar
from urllib.parse import urlparse


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


__all__ = [
    "chunked",
    "dedupe_keep_order",
    "domain_matches",
    "normalize_domain",
    "parse_json_object",
    "strip_markdown_json",
]
