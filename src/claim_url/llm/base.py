"""Provider protocol and the shared retry-driven :class:`LLMClient` facade."""

from __future__ import annotations

import logging
import random
import time
from typing import Optional, Protocol

from claim_url.config import LLMProvider
from claim_url.errors import ConfigError, LLMError


LOG = logging.getLogger("claim-url-finder")


class _Provider(Protocol):
    """Internal provider interface — one concrete impl per backend."""

    model: str

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str: ...


def _backoff_seconds(attempt: int, *, base: float = 1.0, cap: float = 10.0) -> float:
    """Exponential backoff with jitter, capped at ``cap`` seconds."""
    raw = min(base * (2 ** (attempt - 1)), cap)
    return raw + random.uniform(0.0, 0.25 * raw)


class LLMClient:
    """Provider-agnostic LLM facade with retry + backoff.

    Lazily imports the chosen SDK (``openai``, ``anthropic``, ``google-genai``)
    so installing one backend does not require the others.
    """

    def __init__(
        self,
        provider: LLMProvider | str,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.provider = LLMProvider(provider)
        self._provider_impl: _Provider = self._make_provider(self.provider, model, api_key)

    @property
    def model(self) -> str:
        return self._provider_impl.model

    @staticmethod
    def _make_provider(
        provider: LLMProvider, model: Optional[str], api_key: Optional[str]
    ) -> _Provider:
        if provider is LLMProvider.OPENAI:
            from claim_url.llm.openai_provider import OpenAIProvider

            return OpenAIProvider(model=model, api_key=api_key)

        if provider is LLMProvider.CLAUDE:
            from claim_url.llm.claude_provider import ClaudeProvider

            return ClaudeProvider(model=model, api_key=api_key)

        if provider is LLMProvider.GOOGLE:
            from claim_url.llm.google_provider import GoogleProvider

            return GoogleProvider(model=model, api_key=api_key)

        raise ConfigError(f"Unsupported LLM provider: {provider!r}")

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 3000,
        temperature: float = 0.0,
        json_mode: bool = False,
        retries: int = 3,
    ) -> str:
        """Run a single chat-style completion with bounded retries.

        Raises :class:`~claim_url.errors.LLMError` if every attempt fails.
        """
        if retries < 1:
            raise ValueError("retries must be >= 1")

        last_error: Optional[BaseException] = None
        for attempt in range(1, retries + 1):
            try:
                return self._provider_impl.complete(
                    system=system,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    json_mode=json_mode,
                )
            except Exception as exc:
                last_error = exc
                LOG.warning(
                    "LLM call failed attempt=%d/%d provider=%s error=%s",
                    attempt,
                    retries,
                    self.provider.value,
                    exc,
                )
                if attempt == retries:
                    break
                time.sleep(_backoff_seconds(attempt))

        raise LLMError(
            f"LLM call failed after {retries} attempts (provider={self.provider.value})"
        ) from last_error


__all__ = ["LLMClient"]
