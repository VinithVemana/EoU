"""Claim -> Official-Source URL Finder.

Public API for the ``claim_url`` package. Importing this module is
side-effect free — environment loading and logging configuration happen
in :mod:`claim_url.cli` so that library consumers retain control.
"""

from __future__ import annotations

from claim_url.config import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_GOOGLE_MODEL,
    DEFAULT_OPENAI_MODEL,
    LLMProvider,
)
from claim_url.errors import ConfigError
from claim_url.fetch import PageFetcher
from claim_url.finder import ClaimURLFinder
from claim_url.llm import LLMClient
from claim_url.models import (
    ClaimElement,
    DomainCandidate,
    DomainSpec,
    FinderResult,
    RawHit,
    ScoredURL,
    SearchResult,
)
from claim_url.serp import SerpApiClient
from claim_url.spec_context import SpecContext, build_spec_context

__version__ = "1.0.0"

__all__ = [
    "ClaimElement",
    "ClaimURLFinder",
    "ConfigError",
    "DEFAULT_CLAUDE_MODEL",
    "DEFAULT_GOOGLE_MODEL",
    "DEFAULT_OPENAI_MODEL",
    "DomainCandidate",
    "DomainSpec",
    "FinderResult",
    "LLMClient",
    "LLMProvider",
    "PageFetcher",
    "RawHit",
    "ScoredURL",
    "SearchResult",
    "SerpApiClient",
    "SpecContext",
    "build_spec_context",
    "__version__",
]
