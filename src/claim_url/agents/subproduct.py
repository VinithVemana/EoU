"""Identify the sub-products / feature surfaces of a product that are
relevant to a given patent claim.

Generic — no product-specific hardcoding. The LLM is asked to enumerate
the sub-product / API / module / feature surfaces of ``{product}`` whose
official documentation is most likely to evidence the limitations of
``{claim}``. For coherent single-product cases (no meaningful sub-surface)
the LLM returns a single entry covering the whole product.

Output is consumed by :class:`~claim_url.agents.rewriter.QueryRewriteAgent`
to (a) bias query vocabulary toward the relevant surfaces and
(b) guarantee at least one query targets each surface — fixing the
"umbrella product" failure where the rewriter defaults to the most
common sub-product and ignores the rest.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from claim_url.llm import LLMClient
from claim_url.models import DomainCandidate
from claim_url.utils import dedupe_keep_order, parse_json_object


LOG = logging.getLogger("claim-url-finder")


SYSTEM_PROMPT = (
    "You map patent claims to the sub-product / API / feature surfaces of a "
    "product whose docs would evidence the claim's limitations. Always return "
    "valid JSON."
)

PROMPT_TEMPLATE = """\
Product: {product}

Official domains being searched:
{domains_json}

Patent claim (canonical source of truth — read the entire claim, not just keywords):
\"\"\"
{claim}
\"\"\"

Task:
Identify which sub-products, APIs, SDKs, modules, services, or feature surfaces
of {product} are most likely to host official documentation evidencing the
limitations of this claim.

Guidance:
- Read the full claim and infer the technical domain / use-case the claim
  describes (e.g. authentication, streaming, search ranking, fleet dispatch,
  database replication, payment flow, accessibility — whatever fits).
- Then map that use-case onto the sub-surfaces of {product}.
- Many large platforms have several sub-products that share top-level branding
  but address very different use-cases. Pick the surfaces that match the claim,
  not the most popular surfaces overall.
- If {product} is a single coherent product with no meaningful sub-surfaces,
  return a single entry that covers the whole product.
- Return between 1 and {max_subproducts} entries.

For each entry provide:
- name: canonical sub-product / surface name as it appears in vendor docs.
- vocabulary: 2-6 short tokens that frequently appear in vendor docs for this
  surface. These are the rewriter's seed terms — pick distinctive ones, not
  generic words.
- rationale: one sentence on why this surface matches the claim.

Return JSON only using this schema:
{{
  "subproducts": [
    {{
      "name": "...",
      "vocabulary": ["...", "..."],
      "rationale": "..."
    }}
  ]
}}
"""


@dataclass(slots=True)
class SubProduct:
    name: str
    vocabulary: list[str] = field(default_factory=list)
    rationale: str = ""


class SubProductAgent:
    def __init__(
        self,
        llm: LLMClient,
        *,
        max_subproducts: int = 8,
    ) -> None:
        if max_subproducts < 1:
            raise ValueError("max_subproducts must be >= 1")
        self._llm = llm
        self.max_subproducts = max_subproducts

    def discover(
        self,
        *,
        product: str,
        claim: str,
        domains: list[DomainCandidate],
    ) -> list[SubProduct]:
        """Return relevant sub-product surfaces. Empty list on failure."""
        domains_payload = [
            {"domain": d.domain, "rationale": d.rationale} for d in domains
        ]
        prompt = PROMPT_TEMPLATE.format(
            product=product,
            domains_json=json.dumps(domains_payload, indent=2),
            claim=claim.strip(),
            max_subproducts=self.max_subproducts,
        )

        try:
            text = self._llm.complete(
                system=SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=1500,
                temperature=0.0,
                json_mode=True,
            )
            data = parse_json_object(text)
        except Exception as exc:
            LOG.warning("Sub-product probe failed; continuing without it error=%s", exc)
            return []

        raw = data.get("subproducts")
        if not isinstance(raw, list):
            LOG.warning("Sub-product probe returned invalid payload")
            return []

        subproducts: list[SubProduct] = []
        seen: set[str] = set()
        for item in raw:
            sp = self._coerce(item)
            if sp is None:
                continue
            key = sp.name.lower()
            if key in seen:
                continue
            seen.add(key)
            subproducts.append(sp)
            if len(subproducts) >= self.max_subproducts:
                break

        if subproducts:
            LOG.info(
                "Sub-product probe identified %d surfaces: %s",
                len(subproducts),
                ", ".join(sp.name for sp in subproducts),
            )
        return subproducts

    @staticmethod
    def _coerce(item: Any) -> SubProduct | None:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or "").strip()
        if not name:
            return None
        raw_vocab = item.get("vocabulary") or []
        if not isinstance(raw_vocab, list):
            raw_vocab = []
        vocab = dedupe_keep_order(
            str(v).strip() for v in raw_vocab if str(v).strip()
        )[:6]
        return SubProduct(
            name=name,
            vocabulary=vocab,
            rationale=str(item.get("rationale") or "").strip(),
        )


__all__ = ["SubProduct", "SubProductAgent"]
