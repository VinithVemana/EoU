"""Provider protocol and the shared retry-driven :class:`LLMClient` facade."""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Optional, Protocol

from claim_url.cache import DiskCache
from claim_url.config import LLMProvider
from claim_url.errors import ConfigError, LLMError
from claim_url.pricing import UsageStats, lookup_pricing


LOG = logging.getLogger("claim-url-finder")


class _Provider(Protocol):
    """Internal provider interface — one concrete impl per backend.

    ``complete`` returns ``(text, prompt_tokens, completion_tokens)``.
    Returning the token counts inline (instead of via a per-instance
    ``last_usage`` attribute) is required for thread-safety: when the
    facade is shared across worker threads, two concurrent calls would
    otherwise race on the shared attribute.
    """

    model: str

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> tuple[str, int, int]: ...


def _backoff_seconds(attempt: int, *, base: float = 1.0, cap: float = 10.0) -> float:
    """Exponential backoff with jitter, capped at ``cap`` seconds."""
    raw = min(base * (2 ** (attempt - 1)), cap)
    return raw + random.uniform(0.0, 0.25 * raw)


class LLMClient:
    """Provider-agnostic LLM facade with retry + backoff.

    Lazily imports the chosen SDK (``openai``, ``anthropic``, ``google-genai``)
    so installing one backend does not require the others.

    When a :class:`~claim_url.cache.DiskCache` is supplied, deterministic
    completions (``temperature == 0.0``) are read-through cached on disk
    keyed by (provider, model, system, prompt, max_tokens, json_mode).
    Cache hits return instantly with zero API spend; the saved tokens are
    accumulated separately on :class:`UsageStats` so the run summary can
    report what the cache saved.
    """

    def __init__(
        self,
        provider: LLMProvider | str,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        *,
        cache: Optional[DiskCache] = None,
    ) -> None:
        self.provider = LLMProvider(provider)
        self._provider_impl: _Provider = self._make_provider(self.provider, model, api_key)
        self.usage = UsageStats()
        self._pricing = lookup_pricing(self._provider_impl.model)
        self._cache = cache
        self._usage_lock = threading.Lock()

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

    def _cache_key(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode: bool,
    ) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "model": self._provider_impl.model,
            "system": system,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "json_mode": json_mode,
        }

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

        cache_eligible = self._cache is not None and temperature == 0.0
        cache_key: Optional[dict[str, Any]] = None
        if cache_eligible:
            cache_key = self._cache_key(
                system=system, prompt=prompt, max_tokens=max_tokens, json_mode=json_mode
            )
            cached = self._cache.get(cache_key)  # type: ignore[union-attr]
            if isinstance(cached, dict) and "text" in cached:
                with self._usage_lock:
                    self.usage.record_cache_hit(
                        prompt=int(cached.get("prompt_tokens", 0)),
                        completion=int(cached.get("completion_tokens", 0)),
                        pricing=self._pricing,
                    )
                LOG.debug(
                    "LLM cache hit provider=%s model=%s saved_tokens=%d",
                    self.provider.value,
                    self._provider_impl.model,
                    int(cached.get("prompt_tokens", 0))
                    + int(cached.get("completion_tokens", 0)),
                )
                return str(cached["text"])

        last_error: Optional[BaseException] = None
        for attempt in range(1, retries + 1):
            try:
                result = self._provider_impl.complete(
                    system=system,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    json_mode=json_mode,
                )
                if isinstance(result, tuple) and len(result) == 3:
                    text, prompt_toks, completion_toks = result
                else:
                    # Back-compat: provider returned a bare string
                    text = result  # type: ignore[assignment]
                    prompt_toks, completion_toks = getattr(
                        self._provider_impl, "last_usage", (0, 0)
                    )
                with self._usage_lock:
                    self.usage.record(
                        prompt=prompt_toks,
                        completion=completion_toks,
                        pricing=self._pricing,
                    )
                if cache_eligible and cache_key is not None:
                    self._cache.set(  # type: ignore[union-attr]
                        cache_key,
                        {
                            "text": text,
                            "prompt_tokens": prompt_toks,
                            "completion_tokens": completion_toks,
                        },
                    )
                return text
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
