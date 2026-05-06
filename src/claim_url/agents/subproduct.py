"""Identify the sub-products / feature surfaces of a product that are
relevant to a given patent claim.

Generic — no product-specific hardcoding. The agent is **evidence-based**,
not memory-based:

1. SerpApi probes (parallel) enumerate the actual sub-product catalogue
   advertised on the vendor's official domains — products lists,
   documentation indexes, solutions/services pages, API directories.
2. The LLM is then asked to pick the claim-relevant entries from that
   real catalogue, instead of trying to recall the catalogue from
   training memory.

This fixes the umbrella-product failure where, given just a product name,
the LLM defaults to the most popular sub-products it remembers and omits
niche-but-claim-relevant ones (e.g. Google Maps Platform → Geocoding /
Places, missing Fleet Engine / Mobility / Route Optimization).

Output is consumed by :class:`~claim_url.agents.rewriter.QueryRewriteAgent`
to (a) bias query vocabulary toward the relevant surfaces and
(b) guarantee at least one query targets each surface.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from claim_url._progress import progress
from claim_url.llm import LLMClient
from claim_url.models import DomainCandidate, SearchResult
from claim_url.serp import SerpApiClient
from claim_url.utils import dedupe_keep_order, parse_json_object


LOG = logging.getLogger("claim-url-finder")


# Generic catalogue-enumeration probes. Templates expand against {product}.
# Domain-scoped variants (using site:) run additionally per official domain to
# pull the actual docs index entries.
SUBPRODUCT_PROBE_QUERIES: tuple[str, ...] = (
    "{product} products list",
    "{product} all APIs",
    "{product} services overview",
    "{product} solutions catalog",
    "{product} documentation index",
)

SUBPRODUCT_DOMAIN_PROBE_QUERIES: tuple[str, ...] = (
    "products",
    "documentation overview",
    "all services",
)


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

SerpApi catalogue evidence (real titles + URLs surfaced by enumeration probes
on the official domains — this is the actual menu of sub-products, NOT what
you may recall from training memory):
{evidence_json}

Task:
From the catalogue evidence above, identify which sub-products, APIs, SDKs,
modules, services, or feature surfaces of {product} are most likely to host
official documentation evidencing the limitations of this claim.

Guidance:
- Treat the catalogue evidence as ground truth. Prefer entries that appear
  there. You may add an entry that is not literally in the evidence only if
  you have high confidence it exists on one of the listed domains.
- Read the full claim and infer the technical domain / use-case the claim
  describes (authentication, streaming, search ranking, dispatch, replication,
  payment, accessibility, etc.).
- Then map that use-case onto the sub-surfaces visible in the catalogue.
- Many large platforms have several sub-products that share top-level branding
  but address very different use-cases. Pick surfaces that match the claim's
  use-case, not the most popular surfaces overall. Niche surfaces matter when
  they are the closest semantic match.
- If {product} is a single coherent product with no meaningful sub-surfaces,
  return a single entry covering the whole product.
- Return between 1 and {max_subproducts} entries.

For each entry provide:
- name: canonical sub-product / surface name as it appears in vendor docs.
- vocabulary: 2-6 short tokens that frequently appear in vendor docs for this
  surface (favour distinctive terms; skip generic words).
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
        serp: SerpApiClient | None = None,
        *,
        max_subproducts: int = 8,
        probe_results_per_query: int = 8,
        max_probe_workers: int = 5,
        max_evidence_items: int = 60,
    ) -> None:
        if max_subproducts < 1:
            raise ValueError("max_subproducts must be >= 1")
        self._llm = llm
        self._serp = serp
        self.max_subproducts = max_subproducts
        self.probe_results_per_query = probe_results_per_query
        self.max_probe_workers = max(1, int(max_probe_workers))
        self.max_evidence_items = max(1, int(max_evidence_items))

    def discover(
        self,
        *,
        product: str,
        claim: str,
        domains: list[DomainCandidate],
    ) -> list[SubProduct]:
        """Return relevant sub-product surfaces. Empty list on failure.

        Strategy: enumerate the vendor's actual sub-product catalogue via
        SerpApi probes, then ask the LLM to pick claim-relevant entries from
        that real catalogue. Falls back to memory-only mode (no evidence) when
        the SerpApi client was not provided.
        """
        evidence = self._collect_evidence(product, domains)
        domains_payload = [
            {"domain": d.domain, "rationale": d.rationale} for d in domains
        ]
        prompt = PROMPT_TEMPLATE.format(
            product=product,
            domains_json=json.dumps(domains_payload, indent=2),
            claim=claim.strip(),
            evidence_json=json.dumps(evidence, indent=2)
                if evidence else "[]  (no catalogue evidence available)",
            max_subproducts=self.max_subproducts,
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

    def _collect_evidence(
        self,
        product: str,
        domains: list[DomainCandidate],
    ) -> list[dict[str, str]]:
        """Run SerpApi catalogue-enumeration probes; return condensed evidence.

        Two query families:
          1. Product-anchored ("{product} products list", "{product} all APIs", …)
          2. Domain-anchored ("products site:{domain}", "documentation overview
             site:{domain}", "all services site:{domain}") for each official
             domain — surfaces sub-product landing pages that the product-
             anchored queries may miss for niche surfaces.
        """
        if self._serp is None:
            return []

        queries: list[str] = [t.format(product=product) for t in SUBPRODUCT_PROBE_QUERIES]
        for d in domains:
            for tail in SUBPRODUCT_DOMAIN_PROBE_QUERIES:
                queries.append(f"{tail} site:{d.domain}")
        queries = dedupe_keep_order(queries)
        if not queries:
            return []

        def _probe(q: str) -> tuple[str, list[SearchResult]]:
            try:
                return q, self._serp.search(q, num=self.probe_results_per_query)
            except Exception as exc:
                LOG.warning("Sub-product probe query failed q=%r error=%s", q, exc)
                return q, []

        evidence: list[dict[str, str]] = []
        bar = progress(total=len(queries), desc="Sub-product probes", unit="q")
        try:
            workers = max(1, min(self.max_probe_workers, len(queries)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_probe, q) for q in queries]
                for fut in as_completed(futures):
                    q, results = fut.result()
                    for r in results:
                        evidence.append({
                            "query": q,
                            "url": r.url,
                            "title": r.title,
                            "snippet": r.snippet[:300],
                        })
                    bar.update(1)
        finally:
            bar.close()

        # Deduplicate by URL keeping first occurrence; cap to avoid bloating prompt.
        seen: set[str] = set()
        deduped: list[dict[str, str]] = []
        for item in evidence:
            url = item.get("url") or ""
            if url in seen:
                continue
            seen.add(url)
            deduped.append(item)
            if len(deduped) >= self.max_evidence_items:
                break

        LOG.info(
            "Sub-product evidence: %d unique URLs from %d catalogue probes",
            len(deduped), len(queries),
        )
        return deduped

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
