"""Identify the sub-products / feature surfaces of a product that are
relevant to a given patent claim.

Generic — no product-specific hardcoding. The agent is **evidence-based**
and uses a **two-step harvest** (enumerate then filter):

1. SerpApi probes (parallel) enumerate the actual sub-product catalogue
   advertised on the vendor's official domains — products lists,
   documentation indexes, solutions/services pages, API directories.
2. The highest-authority catalogue / overview pages are fetched and
   their bodies stripped to plain text.
3. **Step A (enumerate):** one LLM call that lists *every* sub-product /
   API / SDK / service it can find in the catalogue evidence + page
   bodies, with no relevance filtering. This survives popular-API bias:
   niche surfaces (e.g. Fleet Engine, On-Demand Rides) make the list
   alongside the obvious ones because the only criterion is "appears in
   the catalogue".
4. **Step B (filter):** a second LLM call ranks the enumeration against
   the claim text and (when available) the pre-classified
   :class:`~claim_url.agents.use_case.UseCase`. The use-case anchor
   forces the filter to prefer surfaces that match the claim's domain
   even when those surfaces are dwarfed by popular APIs in the
   catalogue evidence.

This fixes the umbrella-product failure where, given just a product name,
a single combined enumerate-and-filter LLM call defaults to the most
popular sub-products it remembers and omits niche-but-claim-relevant
ones (e.g. Google Maps Platform → Geocoding / Places, missing Fleet
Engine / Mobility / Route Optimization).

Output is consumed by :class:`~claim_url.agents.rewriter.QueryRewriteAgent`
to (a) bias query vocabulary toward the relevant surfaces and
(b) guarantee at least one query targets each surface.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from claim_url._progress import progress
from claim_url.llm import LLMClient
from claim_url.models import DomainCandidate, SearchResult
from claim_url.serp import SerpApiClient
from claim_url.utils import (
    dedupe_keep_order,
    normalize_domain,
    parse_json_object,
    url_matches_spec,
)

if TYPE_CHECKING:
    from claim_url.agents.use_case import UseCase
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


_USE_CASE_BLOCK = """\

Pre-classified technical use-case for this claim (treat as a hard
preference — surfaces that document this use-case should be ranked above
surfaces that merely share surface-level vocabulary):
- primary: {use_case}
- vocabulary anchors: {anchors_json}
- alternative use-cases worth considering: {alternatives_json}
"""


# ---------------------------------------------------------------------------
# Step A — enumerate every sub-product visible in the catalogue evidence.
# ---------------------------------------------------------------------------

ENUMERATE_SYSTEM_PROMPT = (
    "You enumerate sub-products / APIs / SDKs / services / modules of a "
    "product from catalogue evidence. You are an exhaustive lister — "
    "include EVERY surface visible in the evidence even if it looks niche "
    "or unrelated. Do NOT filter by topical relevance; that is a separate "
    "downstream step. Always return valid JSON."
)

ENUMERATE_PROMPT_TEMPLATE = """\
Product: {product}

Official domains being searched:
{domains_json}

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
List EVERY distinct sub-product, API, SDK, service, module, or feature
surface of {product} that is visible in the catalogue evidence above. Be
exhaustive — include niche / vertical / fleet / dispatch / industry-specific
surfaces alongside the popular ones. Do not filter by topical relevance to
any particular use-case; that filtering happens downstream.

Guidance:
- Treat the evidence + page bodies as ground truth. Prefer entries that
  literally appear there.
- You may add an entry that is not literally in the evidence only when
  you have very high confidence that it is hosted on one of the listed
  official domains (e.g. obvious sibling SDKs implied by an Android
  variant being listed). Mark such entries with `evidenced=false`.
- Aim for completeness, not brevity. If the catalogue lists 25 surfaces
  and you can read them, return all 25.
- Skip generic marketing pages, blog categories, pricing, legal, support,
  and changelog entries. Only list actual sub-product / API / SDK names.

For each entry provide:
- name: canonical sub-product / surface name as it appears in vendor docs.
- vocabulary: 2-6 short tokens that frequently appear in vendor docs for
  this surface (favour distinctive terms; skip generic words).
- evidenced: true if this entry literally appeared in the evidence /
  page bodies; false if you inferred it.

Return JSON only:
{{
  "subproducts": [
    {{
      "name": "...",
      "vocabulary": ["...", "..."],
      "evidenced": true
    }}
  ]
}}
"""


# ---------------------------------------------------------------------------
# Step B — filter the enumeration to the entries most relevant to the claim.
# ---------------------------------------------------------------------------

FILTER_SYSTEM_PROMPT = (
    "You select the sub-products from a pre-enumerated list that are most "
    "likely to host official documentation evidencing the limitations of a "
    "patent claim. Always return valid JSON."
)

FILTER_PROMPT_TEMPLATE = """\
Product: {product}

Official domains being searched:
{domains_json}

Patent claim (canonical source of truth — read the entire claim, not just keywords):
\"\"\"
{claim}
\"\"\"
{spec_context_section}{use_case_section}
Pre-enumerated sub-product surfaces (every surface visible in the catalogue
for {product}; this is the candidate pool — do NOT add new entries that
are not in this list):
{enumeration_json}

Task:
From the pre-enumerated pool above, pick the {max_subproducts} surfaces most
likely to host official documentation evidencing the limitations of this
claim. Output a ranked subset.

Guidance:
- Read the full claim and infer the technical domain / use-case (dispatch,
  authentication, streaming, ranking, replication, payment, accessibility,
  fleet management, …). Use the pre-classified use-case above when present.
- Prefer surfaces whose typical use-case matches the claim's. Niche /
  vertical surfaces (fleet management, on-demand rides, asset tracking,
  driver SDKs, …) matter when they are the closest semantic match — pick
  them over more popular generic surfaces in that case.
- A useful test: would the docs page for THIS surface plausibly contain
  a sentence describing one of the claim's limitations? If yes, include it.
- For each picked surface, pull its rationale from the claim's vocabulary,
  not from the surface description alone.
- If the claim is generic enough that several surfaces qualify equally,
  prefer the more specific surface over the umbrella one.
- Return between 1 and {max_subproducts} entries. Order by relevance (most
  relevant first).

For each entry provide:
- name: copy verbatim from the pre-enumerated list.
- vocabulary: re-emit (possibly trimmed) — 2-6 short tokens distinctive
  to this surface.
- rationale: one sentence on why this surface matches the claim.

Return JSON only:
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


# ---------------------------------------------------------------------------
# Single-step prompt — fallback path when the two-step harvest is disabled
# (kept for backwards compatibility with callers that have not opted in).
# ---------------------------------------------------------------------------

SINGLE_STEP_SYSTEM_PROMPT = (
    "You map patent claims to the sub-product / API / feature surfaces of a "
    "product whose docs would evidence the claim's limitations. Always return "
    "valid JSON."
)

SINGLE_STEP_PROMPT_TEMPLATE = """\
Product: {product}

Official domains being searched:
{domains_json}

Patent claim (canonical source of truth — read the entire claim, not just keywords):
\"\"\"
{claim}
\"\"\"
{spec_context_section}{use_case_section}
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
        max_catalogue_pages: int = 8,
        catalogue_body_chars: int = 8000,
        two_step_harvest: bool = True,
        enumeration_cap: int = 60,
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
        self.two_step_harvest = bool(two_step_harvest)
        self.enumeration_cap = max(self.max_subproducts, int(enumeration_cap))
        # Populated by :meth:`discover`. URLs of catalogue / overview / docs
        # index pages whose bodies were fetched during sub-product harvest.
        # Exposed so downstream stages (index-link harvester) can reuse them
        # as additional anchor sources without re-fetching.
        self.last_catalogue_urls: list[str] = []

    def discover(
        self,
        *,
        product: str,
        claim: str,
        domains: list[DomainCandidate],
        spec_context: Optional[str] = None,
        use_case: "UseCase | None" = None,
    ) -> list[SubProduct]:
        """Return relevant sub-product surfaces. Empty list on failure.

        Strategy:
        1. Enumerate the vendor's actual sub-product catalogue via SerpApi
           probes + catalogue page-body harvest.
        2. When :attr:`two_step_harvest` is on (default), the LLM call is
           split into two stages:

           - **Step A (enumerate):** list every sub-product visible in the
             evidence with no relevance filter — niche surfaces survive
             popular-API bias.
           - **Step B (filter):** rank the enumeration against the claim and
             optional :class:`UseCase`.

           When :attr:`two_step_harvest` is off, a single combined call is
           used (pre-2026-05 behaviour).

        Falls back to memory-only mode (no evidence) when the SerpApi
        client was not provided.

        Parameters
        ----------
        spec_context: optional patent description paragraphs.
        use_case: optional pre-classified
            :class:`~claim_url.agents.use_case.UseCase` from
            :class:`UseCaseAgent` — its anchors steer the filter step
            toward surfaces that match the claim's technical domain.
        """
        evidence = self._collect_evidence(product, domains, spec_context=spec_context)
        catalogue_pages = self._fetch_catalogue_pages(evidence, domains)
        self.last_catalogue_urls = [p["url"] for p in catalogue_pages if p.get("url")]
        domains_payload = [
            {"domain": d.display(), "rationale": d.rationale} for d in domains
        ]
        spec_section = (
            _SPEC_CONTEXT_BLOCK.format(spec_context=spec_context.strip())
            if spec_context and spec_context.strip()
            else ""
        )
        use_case_section = self._format_use_case_section(use_case)

        if self.two_step_harvest:
            enumeration = self._enumerate_step(
                product=product,
                domains_payload=domains_payload,
                evidence=evidence,
                catalogue_pages=catalogue_pages,
            )
            if not enumeration:
                LOG.info(
                    "Sub-product enumeration empty; falling back to single-step prompt"
                )
                return self._single_step(
                    product=product,
                    claim=claim,
                    domains_payload=domains_payload,
                    evidence=evidence,
                    catalogue_pages=catalogue_pages,
                    spec_section=spec_section,
                    use_case_section=use_case_section,
                )
            return self._filter_step(
                product=product,
                claim=claim,
                domains_payload=domains_payload,
                enumeration=enumeration,
                spec_section=spec_section,
                use_case_section=use_case_section,
            )

        return self._single_step(
            product=product,
            claim=claim,
            domains_payload=domains_payload,
            evidence=evidence,
            catalogue_pages=catalogue_pages,
            spec_section=spec_section,
            use_case_section=use_case_section,
        )

    # ------------------------------------------------------------------ #
    # Two-step harvest internals
    # ------------------------------------------------------------------ #

    def _enumerate_step(
        self,
        *,
        product: str,
        domains_payload: list[dict[str, str]],
        evidence: list[dict[str, str]],
        catalogue_pages: list[dict[str, str]],
    ) -> list[SubProduct]:
        """Step A — exhaustive enumeration of every visible sub-product."""
        prompt = ENUMERATE_PROMPT_TEMPLATE.format(
            product=product,
            domains_json=json.dumps(domains_payload, indent=2),
            evidence_json=json.dumps(evidence, indent=2)
                if evidence else "[]  (no catalogue evidence available)",
            catalogue_pages_json=json.dumps(catalogue_pages, indent=2)
                if catalogue_pages else "[]  (no catalogue pages fetched)",
        )

        try:
            text = self._llm.complete(
                system=ENUMERATE_SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=2500,
                temperature=0.0,
                json_mode=True,
            )
            data = parse_json_object(text)
        except Exception as exc:
            LOG.warning("Sub-product enumeration step failed error=%s", exc)
            return []

        raw = data.get("subproducts")
        if not isinstance(raw, list):
            LOG.warning("Sub-product enumeration returned invalid payload")
            return []

        out: list[SubProduct] = []
        seen: set[str] = set()
        for item in raw:
            sp = self._coerce(item)
            if sp is None:
                continue
            key = sp.name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(sp)
            if len(out) >= self.enumeration_cap:
                break
        LOG.info(
            "Sub-product enumeration: %d candidates from catalogue evidence",
            len(out),
        )
        return out

    def _filter_step(
        self,
        *,
        product: str,
        claim: str,
        domains_payload: list[dict[str, str]],
        enumeration: list[SubProduct],
        spec_section: str,
        use_case_section: str,
    ) -> list[SubProduct]:
        """Step B — rank pre-enumerated surfaces against the claim."""
        enumeration_payload = [
            {"name": sp.name, "vocabulary": sp.vocabulary} for sp in enumeration
        ]
        prompt = FILTER_PROMPT_TEMPLATE.format(
            product=product,
            domains_json=json.dumps(domains_payload, indent=2),
            claim=claim.strip(),
            spec_context_section=spec_section,
            use_case_section=use_case_section,
            enumeration_json=json.dumps(enumeration_payload, indent=2),
            max_subproducts=self.max_subproducts,
        )

        try:
            text = self._llm.complete(
                system=FILTER_SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=2000,
                temperature=0.0,
                json_mode=True,
            )
            data = parse_json_object(text)
        except Exception as exc:
            LOG.warning("Sub-product filter step failed; using top of enumeration error=%s", exc)
            return enumeration[: self.max_subproducts]

        raw = data.get("subproducts")
        if not isinstance(raw, list):
            LOG.warning("Sub-product filter returned invalid payload; using enumeration head")
            return enumeration[: self.max_subproducts]

        # Index the enumeration so we can recover vocabulary if the filter
        # step omits or trims it.
        by_name = {sp.name.lower(): sp for sp in enumeration}

        picked: list[SubProduct] = []
        seen: set[str] = set()
        for item in raw:
            sp = self._coerce(item)
            if sp is None:
                continue
            key = sp.name.lower()
            if key in seen:
                continue
            seen.add(key)
            # Restore vocabulary from enumeration when the filter step
            # produced a sparse vocabulary list.
            if not sp.vocabulary and key in by_name:
                sp.vocabulary = by_name[key].vocabulary
            picked.append(sp)
            if len(picked) >= self.max_subproducts:
                break

        if not picked:
            LOG.warning("Sub-product filter step picked nothing; using enumeration head")
            return enumeration[: self.max_subproducts]

        LOG.info(
            "Sub-product probe identified %d surfaces (two-step): %s",
            len(picked),
            ", ".join(sp.name for sp in picked),
        )
        return picked

    # ------------------------------------------------------------------ #
    # Single-step (legacy) path — kept for fallback + back-compat.
    # ------------------------------------------------------------------ #

    def _single_step(
        self,
        *,
        product: str,
        claim: str,
        domains_payload: list[dict[str, str]],
        evidence: list[dict[str, str]],
        catalogue_pages: list[dict[str, str]],
        spec_section: str,
        use_case_section: str,
    ) -> list[SubProduct]:
        prompt = SINGLE_STEP_PROMPT_TEMPLATE.format(
            product=product,
            domains_json=json.dumps(domains_payload, indent=2),
            claim=claim.strip(),
            spec_context_section=spec_section,
            use_case_section=use_case_section,
            evidence_json=json.dumps(evidence, indent=2)
                if evidence else "[]  (no catalogue evidence available)",
            catalogue_pages_json=json.dumps(catalogue_pages, indent=2)
                if catalogue_pages else "[]  (no catalogue pages fetched)",
            max_subproducts=self.max_subproducts,
        )

        try:
            text = self._llm.complete(
                system=SINGLE_STEP_SYSTEM_PROMPT,
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
                "Sub-product probe identified %d surfaces (single-step): %s",
                len(subproducts),
                ", ".join(sp.name for sp in subproducts),
            )
        return subproducts

    # ------------------------------------------------------------------ #
    # Evidence + catalogue helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_use_case_section(use_case: "UseCase | None") -> str:
        if not use_case or not bool(use_case):
            return ""
        return _USE_CASE_BLOCK.format(
            use_case=use_case.use_case or "(unspecified)",
            anchors_json=json.dumps(use_case.anchors),
            alternatives_json=json.dumps(use_case.alternative_use_cases),
        )

    def _collect_evidence(
        self,
        product: str,
        domains: list[DomainCandidate],
        spec_context: Optional[str] = None,
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
            site_target = d.spec().site_query()
            for tail in SUBPRODUCT_DOMAIN_PROBE_QUERIES:
                queries.append(f"{tail} site:{site_target}")

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

        specs = [d.spec() for d in domains]
        if not specs:
            return []

        catalogue_keywords = (
            "products", "product", "apis", "api", "documentation",
            "docs", "services", "service", "overview", "platform",
            "solutions", "catalog",
        )
        # Paths containing these segments are legal/policy/reference pages —
        # they don't list sub-product surfaces and pollute the catalogue body.
        _exclude_segments = frozenset({
            "terms", "legal", "policies", "policy", "tos", "agreement",
            "agreements", "privacy", "pricing", "billing", "support",
            "reference", "changelog", "release-notes", "release_notes",
        })

        scored: list[tuple[float, dict[str, str]]] = []
        for item in evidence:
            url = item.get("url") or ""
            if not url or not any(url_matches_spec(url, s) for s in specs):
                continue
            host = normalize_domain(url) or ""

            try:
                from urllib.parse import urlparse
                path = urlparse(url).path or "/"
            except Exception:
                continue

            segments = [s for s in path.split("/") if s]
            # Skip legal/policy/reference pages — they don't list sub-products.
            if any(s.lower() in _exclude_segments for s in segments):
                continue
            keyword_score = sum(
                1 for s in segments if any(k in s.lower() for k in catalogue_keywords)
            )
            # Ratio formula: shallow paths with catalogue keywords rank highest.
            # Additive keyword_score + depth_score was dominated by deep paths
            # with many keyword-matching segments (e.g. /workspace/docs/api/
            # how-tos/overview scored 3.17 while /maps-products/ scored 1.5).
            score = (1.0 + keyword_score) / (1.0 + len(segments))
            # Developer-doc subdomains (developers.*, docs.*, devdocs.*) host
            # the canonical sub-product index. Boost them so they survive a
            # field of homepage / generic /products tied entries — those tend
            # to be JS-rendered marketing landings whose static HTML carries
            # only a fraction of the real catalogue.
            host_lower = host.lower()
            if (
                host_lower.startswith("developers.")
                or host_lower.startswith("docs.")
                or host_lower.startswith("devdocs.")
            ):
                score += 0.25
            if score <= 0.0:
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
