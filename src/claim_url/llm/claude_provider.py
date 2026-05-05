"""Anthropic Claude Messages provider."""

from __future__ import annotations

import os
from typing import Optional

from claim_url.config import DEFAULT_CLAUDE_MODEL, ENV_ANTHROPIC_KEY
from claim_url.errors import ConfigError


class ClaudeProvider:
    def __init__(self, *, model: Optional[str], api_key: Optional[str]) -> None:
        self.model = model or DEFAULT_CLAUDE_MODEL
        api_key = api_key or os.getenv(ENV_ANTHROPIC_KEY)
        if not api_key:
            raise ConfigError(f"{ENV_ANTHROPIC_KEY} is required for --llm claude")

        try:
            import anthropic
        except ImportError as exc:
            raise ConfigError("anthropic SDK not installed: pip install anthropic") from exc

        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,  # noqa: ARG002 - Claude has no native JSON mode here
    ) -> str:
        response = self._client.messages.create(
            model=self.model,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )

        parts: list[str] = []
        for block in response.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)


__all__ = ["ClaudeProvider"]
