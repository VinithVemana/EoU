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
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from claim_url._progress import progress
from claim_url.llm import LLMClient
from claim_url.models import DomainCandidate, SearchResult
from claim_url.serp import SerpApiClient
from claim_url.utils import dedupe_keep_order, domain_matches, normalize_domain, parse_json_object

if TYPE_CHECKING:
    from claim_url.fetch import PageFetcher


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

# Words too generic to be useful as targeted catalogue probe terms.
_SPEC_KW_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "to", "and", "or", "is", "are", "be",
    "for", "with", "that", "this", "at", "on", "by", "as", "from", "it",
    "its", "not", "said", "wherein", "comprising", "configured", "based",
    "one", "more", "each", "any", "least", "having", "method", "system",
    "device", "computer", "processor", "memory", "data", "information",
    "step", "steps", "further", "also", "using", "used", "may", "can",
    "such", "than", "then", "when", "where", "which", "into", "between",
    "about", "after", "before", "during", "including", "includes", "include",
    "described", "shown", "figure", "embodiment", "present", "invention",
    "example", "according", "user", "first", "second", "third", "provide",
    "provides", "provided", "other", "these", "those", "their", "there",
    "terminal", "address", "location", "network", "mobile", "digital",
    "message", "server", "client", "request", "response", "output", "input",
})


def _spec_keywords(spec_text: str, top_n: int = 5) -> list[str]:
    """Extract high-frequency distinctive terms from spec context.

    Used to build additional targeted SerpApi catalogue probes beyond the
    generic "{product} products list" queries.  For a dispatch/fleet patent
    this surfaces "dispatch", "fleet", "driver", "route" which hit niche
    sub-products that generic probes miss entirely.
    """
    words = re.findall(r"[a-zA-Z]{5,}", spec_text.lower())
    freq = Counter(w for w in words if w not in _SPEC_KW_STOPWORDS)
    return [w for w, _ in freq.most_common(top_n)]


_SPEC_CONTEXT_BLOCK = """\

Patent description context (key technical domain vocabulary — use this to
identify the claim's use-case and map it to the right sub-products; niche
surfaces like fleet management, route optimisation, or dispatch APIs should
be preferred when the spec language matches them, even if they are not the
most prominent entries in the catalogue evidence):
\"\"\"
{spec_context}
\"\"\"
"""


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
{spec_context_section}
SerpApi catalogue evidence (real titles + URLs surfaced by enumeration probes
on the official domains — this is the actual menu of sub-products, NOT what
you may recall from training memory):
{evidence_json}

Catalogue page bodies (stripped text of the highest-authority catalogue /
overview pages on the official domains — these typically render the full
product menu inline. Read these carefully and harvest sub-product names from
them, especially niche entries you might not recall from training):
{catalogue_pages_json}

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
        page_fetcher: "PageFetcher | None" = None,
        *,
        max_subproducts: int = 8,
        probe_results_per_query: int = 8,
        max_probe_workers: int = 5,
        max_evidence_items: int = 60,
        max_catalogue_pages: int = 5,
        catalogue_body_chars: int = 4000,
    ) -> None:
        if max_subproducts < 1:
            raise ValueError("max_subproducts must be >= 1")
        self._llm = llm
        self._serp = serp
        self._page_fetcher = page_fetcher
        self.max_subproducts = max_subproducts
        self.probe_results_per_query = probe_results_per_query
        self.max_probe_workers = max(1, int(max_probe_workers))
        self.max_evidence_items = max(1, int(max_evidence_items))
        self.max_catalogue_pages = max(0, int(max_catalogue_pages))
        self.catalogue_body_chars = max(500, int(catalogue_body_chars))

    def discover(
        self,
        *,
        product: str,
        claim: str,
        domains: list[DomainCandidate],
        spec_context: Optional[str] = None,
    ) -> list[SubProduct]:
        """Return relevant sub-product surfaces. Empty list on failure.

        Strategy: enumerate the vendor's actual sub-product catalogue via
        SerpApi probes, then ask the LLM to pick claim-relevant entries from
        that real catalogue. Falls back to memory-only mode (no evidence) when
        the SerpApi client was not provided.

        When *spec_context* is provided (description paragraphs from the
        patent), two things happen:
        - Additional targeted SerpApi probes are added using distinctive
          terms extracted from the spec (e.g. "dispatch", "fleet" → probes
          hit Fleet Engine pages missed by generic catalogue queries).
        - The spec context is injected into the LLM prompt so it can map
          the technical domain directly onto the right sub-surfaces even
          when those surfaces are underrepresented in the catalogue evidence.
        """
        evidence = self._collect_evidence(product, domains, spec_context=spec_context)
        catalogue_pages = self._fetch_catalogue_pages(evidence, domains)
        domains_payload = [
            {"domain": d.domain, "rationale": d.rationale} for d in domains
        ]
        spec_section = (
            _SPEC_CONTEXT_BLOCK.format(spec_context=spec_context.strip())
            if spec_context and spec_context.strip()
            else ""
        )
        prompt = PROMPT_TEMPLATE.format(
            product=product,
            domains_json=json.dumps(domains_payload, indent=2),
            claim=claim.strip(),
            spec_context_section=spec_section,
            evidence_json=json.dumps(evidence, indent=2)
                if evidence else "[]  (no catalogue evidence available)",
            catalogue_pages_json=json.dumps(catalogue_pages, indent=2)
                if catalogue_pages else "[]  (no catalogue pages fetched)",
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
        spec_context: Optional[str] = None,
    ) -> list[dict[str, str]]:
        """Run SerpApi catalogue-enumeration probes; return condensed evidence.

        Three query families:
          1. Product-anchored ("{product} products list", "{product} all APIs", …)
          2. Domain-anchored ("products site:{domain}", "documentation overview
             site:{domain}", "all services site:{domain}") for each official
             domain — surfaces sub-product landing pages that the product-
             anchored queries may miss for niche surfaces.
          3. Spec-keyword-anchored ("{product} {kw}" for top distinctive terms
             from the patent spec) — when spec_context is provided, these targeted
             probes surface niche sub-products that generic catalogue queries
             miss.  E.g. spec containing "dispatch" / "fleet" / "driver" produces
             "{product} dispatch fleet" which hits Fleet Engine / Mobility pages
             directly rather than relying on them appearing in a generic products
             index.
        """
        if self._serp is None:
            return []

        queries: list[str] = [t.format(product=product) for t in SUBPRODUCT_PROBE_QUERIES]
        for d in domains:
            for tail in SUBPRODUCT_DOMAIN_PROBE_QUERIES:
                queries.append(f"{tail} site:{d.domain}")

        if spec_context and spec_context.strip():
            spec_kws = _spec_keywords(spec_context, top_n=5)
            if spec_kws:
                # One combined probe per pair to avoid too many extra calls
                for i in range(0, len(spec_kws), 2):
                    chunk = " ".join(spec_kws[i:i + 2])
                    queries.append(f"{product} {chunk}")
                LOG.debug(
                    "Sub-product spec probes: added %d queries from keywords %s",
                    len(spec_kws) // 2 + len(spec_kws) % 2,
                    spec_kws,
                )

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

    def _fetch_catalogue_pages(
        self,
        evidence: list[dict[str, str]],
        domains: list[DomainCandidate],
    ) -> list[dict[str, str]]:
        """Fetch the highest-authority catalogue/overview pages from evidence.

        SerpApi returns landing-page URLs, not page bodies. Catalogue index
        pages (e.g. ``mapsplatform.google.com/maps-products/``,
        ``aws.amazon.com/products``) typically render the full sub-product
        menu inline; fetching their bodies surfaces niche surfaces that
        SerpApi titles do not include.

        Heuristic: rank URLs whose domain matches one of the official
        domains by catalogue-likelihood (catalogue-shaped path keywords +
        path shortness), pick top N, fetch with PageFetcher (if provided),
        return ``[{url, title, body_excerpt}]``. No-op when no PageFetcher
        is configured or N is 0.
        """
        if self._page_fetcher is None or self.max_catalogue_pages <= 0:
            return []

        official = [normalize_domain(d.domain) or d.domain for d in domains]
        if not official:
            return []

        catalogue_keywords = (
            "products", "product", "apis", "api", "documentation",
            "docs", "services", "service", "overview", "platform",
            "solutions", "catalog",
        )

        scored: list[tuple[float, dict[str, str]]] = []
        for item in evidence:
            url = item.get("url") or ""
            host = normalize_domain(url) or ""
            if not host or not any(domain_matches(host, d) for d in official):
                continue

            try:
                from urllib.parse import urlparse
                path = urlparse(url).path or "/"
            except Exception:
                continue

            segments = [s for s in path.split("/") if s]
            depth_score = 1.0 / (1 + len(segments))  # shallower = more catalogue-y
            keyword_score = sum(
                1 for s in segments if any(k in s.lower() for k in catalogue_keywords)
            )
            score = keyword_score + depth_score
            if score <= 0.0 and len(segments) > 0:
                continue
            scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        picked = [item for _, item in scored[: self.max_catalogue_pages]]
        if not picked:
            return []

        urls = [item["url"] for item in picked]
        LOG.info(
            "Sub-product catalogue: fetching %d landing pages: %s",
            len(urls), ", ".join(urls),
        )
        bodies = self._page_fetcher.fetch_many(urls)

        out: list[dict[str, str]] = []
        for item in picked:
            url = item["url"]
            body = (bodies.get(url) or "").strip()
            if not body:
                continue
            out.append({
                "url": url,
                "title": item.get("title", ""),
                "body_excerpt": body[: self.catalogue_body_chars],
            })

        LOG.info("Sub-product catalogue: %d pages have non-empty bodies", len(out))
        return out

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
