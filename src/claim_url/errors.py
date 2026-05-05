"""Exception types raised by the :mod:`claim_url` package."""

from __future__ import annotations


class ClaimURLError(RuntimeError):
    """Base class for errors raised by this package."""


class ConfigError(ClaimURLError):
    """Raised for missing or invalid configuration (env vars, args, deps)."""


class LLMError(ClaimURLError):
    """Raised when an LLM call ultimately fails after retries."""


class SearchError(ClaimURLError):
    """Raised when SerpApi calls fail terminally."""


__all__ = ["ClaimURLError", "ConfigError", "LLMError", "SearchError"]
