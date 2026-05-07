"""Classify the technical use-case of a patent claim.

A single LLM call that takes the full claim text plus optional patent
specification context and returns a short label describing the claim's
technical use-case (e.g. ``"vehicle dispatch / fleet routing"``,
``"on-device autocomplete"``, ``"distributed snapshot replication"``)
along with a small set of vocabulary anchor tokens.

The result is shared across downstream stages:

- :class:`~claim_url.agents.subproduct.SubProductAgent` uses it to bias
  catalogue filtering toward niche surfaces matching the use-case rather
  than the most popular sub-products on the catalogue page.
- :class:`~claim_url.agents.rewriter.QueryRewriteAgent` injects the anchors
  into the search-query templates so every emitted query contains at
  least one grounded vocabulary token.

Why this stage exists
---------------------
Without a shared use-case label, every downstream agent re-derives the
claim's domain implicitly from its own slice of context. A multi-API
umbrella product like Google Maps Platform exposes ~20 surfaces; if the
sub-product probe and the rewriter independently guess "this is about
geocoding" vs "this is about routing", their outputs diverge and queries
miss the actual relevant docs (e.g. Fleet Engine for a dispatch claim).

This stage is generic — no product-specific hardcoding. It works for any
patent claim regardless of the product.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from claim_url.llm import LLMClient
from claim_url.utils import dedupe_keep_order, parse_json_object


LOG = logging.getLogger("claim-url-finder")


SYSTEM_PROMPT = (
    "You classify the technical use-case of a patent claim into a short "
    "label and a handful of vocabulary anchors that vendor documentation "
    "would use to describe the same use-case. Always return valid JSON."
)

PROMPT_TEMPLATE = """\
Patent claim (canonical source of truth):
\"\"\"
{claim}
\"\"\"
{spec_context_section}
Task:
Classify this claim into a single technical use-case and return a small set
of vocabulary anchor tokens.

Step 1 (internal) — read the full claim (and the spec context if provided).
Identify the system-level technical domain (e.g. authentication, streaming,
search ranking, dispatch / fleet routing, replication, payment, asset
tracking, voice recognition, content moderation, accessibility, ad
auctioning, autocomplete, …).

Step 2 — emit:
- use_case: short label (2-6 words) naming the technical use-case as a
  vendor would describe it. Avoid patent jargon; prefer the term a
  product-marketing team would use.
- anchors: 3-6 short vocabulary tokens (single words or 2-word phrases)
  that vendor documentation pages for this use-case actually contain.
  Favour distinctive terms over generic ones (e.g. "dispatch", "fleet",
  "driver" beat "data", "system", "device").
- alternative_use_cases: 0-2 additional related labels worth considering
  if the primary use-case is not visible in the catalogue evidence later.

Rules:
- Use the spec context when available — it almost always names the
  use-case in plain language even when the claim hides it behind generic
  terminology ("first terminal", "second device", "build string", …).
- If the claim is genuinely generic (a single coherent use-case applies),
  return a single use_case and leave alternative_use_cases empty.
- Return JSON only.

Schema:
{{
  "use_case": "...",
  "anchors": ["...", "..."],
  "alternative_use_cases": ["..."]
}}
"""


_SPEC_CONTEXT_BLOCK = """\

Patent description context (concrete implementation vocabulary — read this
to anchor the use-case in the words the inventor actually uses, not the
abstract claim language):
\"\"\"
{spec_context}
\"\"\"
"""


@dataclass(slots=True)
class UseCase:
    """Classification of a claim's technical use-case."""

    use_case: str = ""
    anchors: list[str] = field(default_factory=list)
    alternative_use_cases: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.use_case) or bool(self.anchors)

    def all_labels(self) -> list[str]:
        """Primary use-case followed by alternatives (empty entries dropped)."""
        labels = [self.use_case] + list(self.alternative_use_cases)
        return [label for label in labels if label]


class UseCaseAgent:
    """Single-LLM-call classifier of a claim's technical use-case."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def classify(
        self,
        *,
        claim: str,
        spec_context: Optional[str] = None,
    ) -> UseCase:
        """Return a :class:`UseCase` for *claim*. Empty on LLM/JSON failure."""
        if not claim or not claim.strip():
            raise ValueError("claim is required")

        spec_section = (
            _SPEC_CONTEXT_BLOCK.format(spec_context=spec_context.strip())
            if spec_context and spec_context.strip()
            else ""
        )
        prompt = PROMPT_TEMPLATE.format(
            claim=claim.strip(),
            spec_context_section=spec_section,
        )

        try:
            text = self._llm.complete(
                system=SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=400,
                temperature=0.0,
                json_mode=True,
            )
            data = parse_json_object(text)
        except Exception as exc:
            LOG.warning("Use-case classification failed; continuing without it error=%s", exc)
            return UseCase()

        use_case = str(data.get("use_case") or "").strip()
        raw_anchors = data.get("anchors") or []
        if not isinstance(raw_anchors, list):
            raw_anchors = []
        anchors = dedupe_keep_order(
            str(a).strip() for a in raw_anchors if str(a).strip()
        )[:6]

        raw_alt = data.get("alternative_use_cases") or []
        if not isinstance(raw_alt, list):
            raw_alt = []
        alternatives = dedupe_keep_order(
            str(a).strip() for a in raw_alt if str(a).strip()
        )[:2]

        result = UseCase(
            use_case=use_case,
            anchors=anchors,
            alternative_use_cases=alternatives,
        )
        if result:
            LOG.info(
                "Use-case classified: %r anchors=%s alternatives=%s",
                result.use_case, result.anchors, result.alternative_use_cases,
            )
        return result


__all__ = ["UseCase", "UseCaseAgent"]
