"""Static configuration: provider enum, default models, env-var names."""

from __future__ import annotations

from enum import Enum


class LLMProvider(str, Enum):
    OPENAI = "openai"
    CLAUDE = "claude"
    GOOGLE = "google"


DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_GOOGLE_MODEL = "gemini-2.5-pro"

ENV_SERPAPI_KEY = "SERPAPI_API_KEY"
ENV_OPENAI_KEY = "OPENAI_API_KEY"
ENV_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
ENV_GOOGLE_KEY = "GOOGLE_API_KEY"

ENV_PCS_API_KEY = "PCS_API_KEY"
ENV_PCS_BASE_URL = "PCS_API_BASE_URL"
ENV_PCS_PORT = "PCS_API_PORT"


LOGGER_NAME = "claim-url-finder"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s - %(message)s"
DEFAULT_LOG_FILE = "claim_url.log"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

DOMAIN_PROBE_QUERIES: tuple[str, ...] = (
    "{product} official website",
    "{product} official support",
    "{product} documentation official",
    "{product} help center official",
    "{product} official blog newsroom",
)


__all__ = [
    "DEFAULT_CLAUDE_MODEL",
    "DEFAULT_GOOGLE_MODEL",
    "DEFAULT_LOG_FILE",
    "DEFAULT_OPENAI_MODEL",
    "DOMAIN_PROBE_QUERIES",
    "ENV_ANTHROPIC_KEY",
    "ENV_GOOGLE_KEY",
    "ENV_OPENAI_KEY",
    "ENV_PCS_API_KEY",
    "ENV_PCS_BASE_URL",
    "ENV_PCS_PORT",
    "ENV_SERPAPI_KEY",
    "LLMProvider",
    "LOGGER_NAME",
    "LOG_FORMAT",
    "USER_AGENT",
]
