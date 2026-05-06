"""Suggest candidate commercial products from a patent claim.

Used by the CLI when ``--product`` is omitted: the LLM is asked to
nominate well-known shipping products this claim could plausibly read on
so the user can pick one before the rest of the pipeline runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from claim_url.llm import LLMClient
from claim_url.utils import parse_json_object


LOG = logging.getLogger("claim-url-finder")


SYSTEM_PROMPT = (
    "You are a patent licensing analyst. Identify real, currently-shipping "
    "commercial products this claim could plausibly read on. Return valid JSON only."
)

PROMPT_TEMPLATE = """\
Patent claim:
\"\"\"
{claim}
\"\"\"

Suggest {n} distinct, well-known commercial products this claim could plausibly
describe behavior of. Prefer mainstream products with public official documentation
(help center, support pages, vendor blog).

Rules:
- Each suggestion must be a specific named product, not a category.
- Include the vendor or parent company.
- Give a one-line rationale tying the product to the claim.

Return JSON only:
{{
  "products": [
    {{"name": "Product Name", "vendor": "Vendor", "rationale": "1-line reason"}}
  ]
}}
"""


@dataclass(slots=True)
class ProductSuggestion:
    name: str
    vendor: str = ""
    rationale: str = ""


class ProductSuggestionAgent:
    def __init__(self, llm: LLMClient, *, max_suggestions: int = 7) -> None:
        if max_suggestions < 1:
            raise ValueError("max_suggestions must be >= 1")
        self._llm = llm
        self.max_suggestions = max_suggestions

    def suggest(self, claim: str) -> list[ProductSuggestion]:
        if not claim or not claim.strip():
            raise ValueError("claim text is required")

        text = self._llm.complete(
            system=SYSTEM_PROMPT,
            prompt=PROMPT_TEMPLATE.format(claim=claim.strip(), n=self.max_suggestions),
            max_tokens=1500,
            temperature=0.0,
            json_mode=True,
        )
        try:
            data = parse_json_object(text)
        except Exception as exc:
            LOG.warning("Product suggestion parse failed: %s", exc)
            return []

        raw = data.get("products")
        if not isinstance(raw, list):
            return []
        return self._coerce(raw)

    def _coerce(self, raw: list[Any]) -> list[ProductSuggestion]:
        seen: set[str] = set()
        out: list[ProductSuggestion] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ProductSuggestion(
                    name=name,
                    vendor=str(item.get("vendor") or "").strip(),
                    rationale=str(item.get("rationale") or "").strip(),
                )
            )
        return out[: self.max_suggestions]


__all__ = ["ProductSuggestion", "ProductSuggestionAgent"]
