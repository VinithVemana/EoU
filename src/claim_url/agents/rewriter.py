"""Translate patent-jargon claim limitations into product-vocabulary search queries.

This step is load-bearing for recall. Issuing raw patent terminology to
narrow ``site:`` queries against vendor docs returns near-zero hits;
rewriting into product-feature vocabulary closes the gap.
"""

from __future__ import annotations

import json
import logging

from claim_url.llm import LLMClient
from claim_url.models import ClaimElement, DomainCandidate
from claim_url.utils import dedupe_keep_order, parse_json_object


LOG = logging.getLogger("claim-url-finder")


SYSTEM_PROMPT = (
    "You translate patent claim limitations into Google search queries that surface "
    "official product documentation. Always return valid JSON."
)

PROMPT_TEMPLATE = """\
Product: {product}

Official domains being searched (with vendor evidence Agent 1 saw):
{domains_json}

Claim elements:
{elements_json}

For each claim element, generate {n} distinct Google search queries that would surface the official product documentation page describing that feature on the listed domains.

Critical translation step:
Patent claims describe behaviour abstractly. Vendor docs use feature names. Translate patent jargon into the product's actual user-facing vocabulary before emitting the query.

Examples of the kind of translation expected:
- "incremental keystrokes from input device" -> "search suggestions" / "autocomplete" / "type to search"
- "build a string from keystrokes" -> "search bar" / "remote keyboard"
- "error model" / "ambiguous keystrokes" -> "search corrections" / "did you mean" / "voice search"
- "catalog of items in memory" -> "library" / "watchlist" / "channel guide"
- "ordering items on a display" -> "home screen" / "recommendations" / "lineup"

Rules:
- 3-7 tokens per query.
- Each element's {n} queries must be distinct: different synonyms, angles, or anchors.
- Do NOT include site: operators. Domain restriction is added by the caller.
- Do NOT wrap the product name in quotes.
- Anchor with the product or its short name when it improves precision; omit when the feature name alone is more natural.
- If the element is generic boilerplate, pick the closest concrete product feature.
- Return JSON only.

Schema:
{{
  "elements": [
    {{
      "id": "E1",
      "queries": ["query 1", "query 2", "query 3"]
    }}
  ]
}}
"""


class QueryRewriteAgent:
    def __init__(self, llm: LLMClient, *, queries_per_element: int = 3) -> None:
        if queries_per_element < 1:
            raise ValueError("queries_per_element must be >= 1")
        self._llm = llm
        self.queries_per_element = queries_per_element

    def rewrite(
        self,
        *,
        product: str,
        elements: list[ClaimElement],
        domains: list[DomainCandidate],
    ) -> list[ClaimElement]:
        """Mutates ``elements`` in place with rewritten queries; returns the same list.

        Falls back to the keyword-only query when rewriting fails so the
        pipeline always has *something* to search for each element.
        """
        if not elements:
            return elements

        domains_payload = [
            {"domain": d.domain, "rationale": d.rationale, "source_urls": d.source_urls[:3]}
            for d in domains
        ]
        elements_payload = [
            {"id": e.id, "label": e.label, "keywords": e.keywords} for e in elements
        ]

        prompt = PROMPT_TEMPLATE.format(
            product=product,
            domains_json=json.dumps(domains_payload, indent=2),
            elements_json=json.dumps(elements_payload, indent=2),
            n=self.queries_per_element,
        )

        try:
            text = self._llm.complete(
                system=SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=2000,
                temperature=0.0,
                json_mode=True,
            )
            data = parse_json_object(text)
        except Exception as exc:
            LOG.warning("Query rewrite failed; falling back to keyword queries error=%s", exc)
            return elements

        raw_elements = data.get("elements")
        if not isinstance(raw_elements, list):
            LOG.warning("Query rewrite returned invalid payload; falling back")
            return elements

        by_id: dict[str, list[str]] = {}
        for item in raw_elements:
            if not isinstance(item, dict):
                continue
            element_id = str(item.get("id") or "").strip()
            if not element_id:
                continue
            queries = item.get("queries") or []
            if not isinstance(queries, list):
                continue
            cleaned = dedupe_keep_order(
                str(q).strip() for q in queries if str(q).strip()
            )[: self.queries_per_element]
            if cleaned:
                by_id[element_id] = cleaned

        for element in elements:
            element.search_queries = by_id.get(element.id, [])
            if element.search_queries:
                LOG.debug(
                    "Rewritten queries element=%s queries=%s",
                    element.id,
                    element.search_queries,
                )
            else:
                LOG.warning(
                    "No rewritten queries for element=%s; will use keyword fallback",
                    element.id,
                )
        return elements


__all__ = ["QueryRewriteAgent"]
