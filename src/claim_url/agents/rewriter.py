"""Translate patent-jargon claim limitations into product-vocabulary search queries.

This step is load-bearing for recall. Issuing raw patent terminology to
narrow ``site:`` queries against vendor docs returns near-zero hits;
rewriting into product-feature vocabulary closes the gap.

The rewriter is given the **full claim text** in addition to the decomposed
elements. Element labels are paraphrases — system-level context (the
overall use-case of the claim) is lost in extraction. Passing the raw
claim lets the LLM correctly frame which sub-surface of a multi-product
platform the claim targets before emitting queries.

When sub-products are provided (from
:class:`~claim_url.agents.subproduct.SubProductAgent`), the rewriter must
distribute queries so every listed surface receives at least one query
across the full element set, AND no single surface may dominate more
than its fair share of the total query budget. This stops queries from
clustering on the most popular sub-product and starving the rest.

Every emitted query must contain at least one anchor token: a sub-product
name, a use-case anchor token, the product name, or the vendor brand.
Orphan jargon queries (e.g. ``"dispatch memory location data"``) match
zero pages on a narrow ``site:`` filter and waste the SerpApi budget.
"""

from __future__ import annotations

import json
import logging
import math
from typing import TYPE_CHECKING, Optional

from claim_url.llm import LLMClient
from claim_url.models import ClaimElement, DomainCandidate
from claim_url.utils import dedupe_keep_order, parse_json_object

if TYPE_CHECKING:
    from claim_url.agents.subproduct import SubProduct
    from claim_url.agents.use_case import UseCase
    from claim_url.spec_context import SpecContext


LOG = logging.getLogger("claim-url-finder")


SYSTEM_PROMPT = (
    "You translate patent claim limitations into Google search queries that surface "
    "official product documentation. Always return valid JSON."
)

PROMPT_TEMPLATE = """\
Product: {product}

Official domains being searched (with vendor evidence Agent 1 saw):
{domains_json}

Sub-product / feature surfaces of {product} that are relevant to this claim
(pre-identified — distribute your queries so each surface listed here is
targeted by at least one query somewhere across the full element set, AND
no single surface may dominate more than {per_surface_cap} of your queries):
{subproducts_json}
{use_case_section}
Full patent claim (canonical context — read the whole claim before emitting
queries; element labels alone lose system-level framing):
\"\"\"
{claim}
\"\"\"
{spec_context_section}
Claim elements (decomposed limitations):
{elements_json}

Step 1 (internal) — Identify the technical domain / use-case the claim describes
(authentication, streaming, search ranking, dispatch, replication, payment,
accessibility, etc.). Use the full claim text — element labels are paraphrases.
If a pre-classified use-case is supplied above, prefer its anchors over your
own guess.

Step 2 — For each claim element, generate {n} distinct Google search queries
that would surface the official documentation page describing that limitation
on the listed domains, using vocabulary appropriate to the use-case from step 1.

Critical translation:
Patent claims describe behaviour abstractly with generic terms ("first terminal",
"build string", "incremental keystrokes", "second device"). Vendor docs use
feature names. Translate generic claim language into the product's actual
user-facing vocabulary before emitting the query — generic terms as literal
queries return near-zero hits on narrow site: filters.

Examples of the kind of translation expected (illustrative, not exhaustive):
- "incremental keystrokes from input device" -> "search suggestions" / "autocomplete"
- "build a string from keystrokes" -> "search bar" / "remote keyboard"
- "error model" / "ambiguous keystrokes" -> "search corrections" / "did you mean"
- "catalog of items in memory" -> "library" / "watchlist" / "channel guide"
- "ordering items on a display" -> "home screen" / "recommendations" / "lineup"
- "first terminal / second terminal exchanging location data" -> sub-product
  vocabulary appropriate to the claim's use-case (could be fleet dispatch,
  ride-sharing, asset tracking, family location-sharing, etc. — the use-case
  determines the right vocabulary)

Anchor rule (mandatory):
Every query MUST contain at least one of:
  (a) a sub-product / surface name from the list above, or
  (b) a use-case anchor token from the list above (when supplied), or
  (c) the product name "{product}" or its vendor / brand.
Orphan jargon queries that contain none of these (e.g. literal patent
terms like "dispatch memory location data") match zero results on
narrow site: filters and are forbidden.

Per-surface query cap:
If a sub-product list is given above, no single sub-product may appear in
more than {per_surface_cap} of the total queries you emit across all
elements. The union of all queries must still cover every listed surface
at least once. Spread coverage; do not concentrate.

Other rules:
- 3-7 tokens per query.
- Each element's {n} queries must be distinct: different synonyms, angles, or anchors.
- Do NOT include site: operators. Domain restriction is added by the caller.
- Do NOT wrap the product name in quotes.
- Anchor with the product or sub-product name when it improves precision.
- If the element is generic boilerplate, pick the closest concrete feature.
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


_SPEC_CONTEXT_BLOCK = """\
Relevant patent description context (technical implementation detail behind
the claim — use this to pick product vocabulary matching what vendors actually
call these features; e.g. spec says "ranking by predicted engagement" when
claim says "ordering items by probability measure"):
\"\"\"
{spec_context}
\"\"\"
"""


_USE_CASE_BLOCK = """\

Pre-classified technical use-case for this claim (treat its anchors as the
preferred vocabulary tokens — at least one of these tokens, OR a sub-product
name, OR the product/vendor brand should appear in every emitted query):
- primary: {use_case}
- vocabulary anchors: {anchors_json}
- alternative use-cases worth considering: {alternatives_json}
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
        claim: str,
        elements: list[ClaimElement],
        domains: list[DomainCandidate],
        subproducts: Optional[list["SubProduct"]] = None,
        spec_context: Optional[str] = None,
        use_case: "UseCase | None" = None,
    ) -> list[ClaimElement]:
        """Mutates ``elements`` in place with rewritten queries; returns the same list.

        Falls back to the keyword-only query when rewriting fails so the
        pipeline always has *something* to search for each element.

        Parameters
        ----------
        use_case: optional pre-classified
            :class:`~claim_url.agents.use_case.UseCase`. When provided, its
            anchors are added to the prompt as preferred vocabulary tokens.
        """
        if not elements:
            return elements

        domains_payload = [
            {"domain": d.display(), "rationale": d.rationale, "source_urls": d.source_urls[:3]}
            for d in domains
        ]
        elements_payload = [
            {"id": e.id, "label": e.label, "keywords": e.keywords} for e in elements
        ]
        subproducts_payload = [
            {"name": sp.name, "vocabulary": sp.vocabulary, "rationale": sp.rationale}
            for sp in (subproducts or [])
        ]

        # Per-surface cap: ceil(total_queries / num_surfaces). Stops a single
        # surface from absorbing all queries while still allowing concentration
        # when there are very few surfaces relative to the query budget.
        total_queries = max(1, len(elements) * self.queries_per_element)
        num_surfaces = max(1, len(subproducts_payload))
        per_surface_cap = max(1, math.ceil(total_queries / num_surfaces))

        spec_section = (
            _SPEC_CONTEXT_BLOCK.format(spec_context=spec_context.strip())
            if spec_context and spec_context.strip()
            else ""
        )
        use_case_section = self._format_use_case_section(use_case)
        prompt = PROMPT_TEMPLATE.format(
            product=product,
            claim=claim.strip(),
            domains_json=json.dumps(domains_payload, indent=2),
            subproducts_json=json.dumps(subproducts_payload, indent=2)
                if subproducts_payload else "[]  (none provided)",
            use_case_section=use_case_section,
            spec_context_section=spec_section,
            elements_json=json.dumps(elements_payload, indent=2),
            n=self.queries_per_element,
            per_surface_cap=per_surface_cap,
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

    @staticmethod
    def _format_use_case_section(use_case: "UseCase | None") -> str:
        if not use_case or not bool(use_case):
            return ""
        return _USE_CASE_BLOCK.format(
            use_case=use_case.use_case or "(unspecified)",
            anchors_json=json.dumps(use_case.anchors),
            alternatives_json=json.dumps(use_case.alternative_use_cases),
        )


__all__ = ["QueryRewriteAgent"]
