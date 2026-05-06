"""Decompose a patent claim into discrete technical limitations."""

from __future__ import annotations

import logging
from typing import Optional

from claim_url.errors import ClaimURLError
from claim_url.llm import LLMClient
from claim_url.models import ClaimElement
from claim_url.utils import parse_json_object


LOG = logging.getLogger("claim-url-finder")


SYSTEM_PROMPT = "You are a careful patent analyst. Always return valid JSON."

PROMPT_TEMPLATE = """\
Decompose the following patent claim into 4-8 discrete technical limitations.

For each element output:
- id: short stable id like "E1", "E2", ...
- label: one-sentence plain-English description
- keywords: 3-6 search-friendly keywords or phrases likely to surface product documentation

Rules:
- Do not include legal boilerplate as an element unless it contains a technical limitation.
- Prefer searchable product-behavior phrases.
- Return JSON only.

Schema:
{{
  "elements": [
    {{
      "id": "E1",
      "label": "...",
      "keywords": ["...", "..."]
    }}
  ]
}}

CLAIM:
\"\"\"
{claim}
\"\"\"
{spec_context_section}"""

_SPEC_CONTEXT_BLOCK = """\

Additional context from the patent description (use technical terms and
implementation detail below to produce more precise element labels and keywords —
do not copy text verbatim; let it inform vocabulary choices):
\"\"\"
{spec_context}
\"\"\"
"""


class ClaimElementExtractor:
    """Deterministic LLM-driven extractor (not an autonomous agent)."""

    MAX_KEYWORDS = 8
    MAX_FALLBACK_KEYWORDS = 6

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def extract(self, claim: str, *, spec_context: Optional[str] = None) -> list[ClaimElement]:
        if not claim or not claim.strip():
            raise ValueError("claim text is required")

        spec_section = (
            _SPEC_CONTEXT_BLOCK.format(spec_context=spec_context.strip())
            if spec_context and spec_context.strip()
            else ""
        )
        text = self._llm.complete(
            system=SYSTEM_PROMPT,
            prompt=PROMPT_TEMPLATE.format(claim=claim, spec_context_section=spec_section),
            max_tokens=2500,
            temperature=0.0,
            json_mode=True,
        )
        data = parse_json_object(text)
        raw_elements = data.get("elements")
        if not isinstance(raw_elements, list) or not raw_elements:
            raise ClaimURLError("LLM did not return any claim elements")

        elements: list[ClaimElement] = []
        for idx, item in enumerate(raw_elements, start=1):
            element = self._coerce_element(item, idx)
            if element is not None:
                elements.append(element)

        if not elements:
            raise ClaimURLError("No valid claim elements extracted")
        return elements

    @classmethod
    def _coerce_element(cls, item: object, idx: int) -> ClaimElement | None:
        if not isinstance(item, dict):
            return None

        label = str(item.get("label") or "").strip()
        if not label:
            return None

        element_id = str(item.get("id") or f"E{idx}").strip() or f"E{idx}"
        raw_keywords = item.get("keywords") or []
        if not isinstance(raw_keywords, list):
            raw_keywords = []

        keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
        if not keywords:
            keywords = label.split()[: cls.MAX_FALLBACK_KEYWORDS]

        return ClaimElement(
            id=element_id,
            label=label,
            keywords=keywords[: cls.MAX_KEYWORDS],
        )


__all__ = ["ClaimElementExtractor"]
