"""OpenAI Chat Completions provider."""

from __future__ import annotations

import os
from typing import Any, Optional

from claim_url.config import DEFAULT_OPENAI_MODEL, ENV_OPENAI_KEY
from claim_url.errors import ConfigError


_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _uses_max_completion_tokens(model: str) -> bool:
    """OpenAI reasoning + gpt-5.x reject ``max_tokens`` and require ``max_completion_tokens``."""
    if not model:
        return False
    name = model.lower()
    return any(name.startswith(prefix) for prefix in _REASONING_PREFIXES)


class OpenAIProvider:
    def __init__(self, *, model: Optional[str], api_key: Optional[str]) -> None:
        self.model = model or DEFAULT_OPENAI_MODEL
        api_key = api_key or os.getenv(ENV_OPENAI_KEY)
        if not api_key:
            raise ConfigError(f"{ENV_OPENAI_KEY} is required for --llm openai")

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ConfigError("openai SDK not installed: pip install openai") from exc

        self._client = OpenAI(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> tuple[str, int, int]:
        token_kwarg = "max_completion_tokens" if _uses_max_completion_tokens(self.model) else "max_tokens"

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            token_kwarg: max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            # Fallback once if the API rejects the chosen token kwarg name.
            error_text = str(exc)
            if "max_tokens" in error_text or "max_completion_tokens" in error_text:
                other = "max_tokens" if token_kwarg == "max_completion_tokens" else "max_completion_tokens"
                kwargs.pop(token_kwarg, None)
                kwargs[other] = max_tokens
                response = self._client.chat.completions.create(**kwargs)
            else:
                raise

        usage = getattr(response, "usage", None)
        prompt_toks = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_toks = int(getattr(usage, "completion_tokens", 0) or 0)
        return response.choices[0].message.content or "", prompt_toks, completion_toks


__all__ = ["OpenAIProvider"]
