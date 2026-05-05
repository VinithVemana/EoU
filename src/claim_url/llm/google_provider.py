"""Google Gemini provider."""

from __future__ import annotations

import os
from typing import Optional

from claim_url.config import DEFAULT_GOOGLE_MODEL, ENV_GOOGLE_KEY
from claim_url.errors import ConfigError


class GoogleProvider:
    def __init__(self, *, model: Optional[str], api_key: Optional[str]) -> None:
        self.model = model or DEFAULT_GOOGLE_MODEL
        api_key = api_key or os.getenv(ENV_GOOGLE_KEY)
        if not api_key:
            raise ConfigError(f"{ENV_GOOGLE_KEY} is required for --llm google")

        try:
            from google import genai
        except ImportError as exc:
            raise ConfigError("google-genai SDK not installed: pip install google-genai") from exc

        self._client = genai.Client(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str:
        from google.genai import types

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json" if json_mode else "text/plain",
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=f"{system}\n\n{prompt}",
            config=config,
        )
        return response.text or ""


__all__ = ["GoogleProvider"]
