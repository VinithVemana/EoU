"""Agent 2: score candidate URLs against the claim and its decomposed elements.

Receives the full claim text *and* the decomposed elements. The decomposition
loses context; the full claim lets the model make associative jumps
("recommendations" <-> "presenting most likely items") that the strict
per-element rubric otherwise rejects.

When ``RawHit.body`` is populated by :class:`~claim_url.fetch.PageFetcher`,
it is included in the candidate payload — pages whose feature-specific
vocabulary lives in the body score much higher than from snippet alone.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from claim_url._progress import progress
from claim_url.llm import LLMClient
from claim_url.models import ClaimElement, RawHit, ScoredURL
from claim_url.utils import chunked, dedupe_keep_order, parse_json_object


LOG = logging.getLogger("claim-url-finder")


SYSTEM_PROMPT = """\
You are a patent claim charting analyst building an evidence list.

Your job is to surface official product documentation that may serve as evidence for any limitation in a patent claim. Recall matters: a human will review the shortlist. Do not be excessively strict — pages that describe the same product behaviour using different vocabulary are valid evidence.

Return valid JSON only.
"""


PROMPT_TEMPLATE = """\
Product:
{product}

Full patent claim (canonical source of truth — use this for associative semantic matching):
\"\"\"
{claim}
\"\"\"

Claim elements (decomposed limitations; ids are stable):
{elements_json}

Candidate official URLs:
{candidates_json}

For each candidate URL:
1. Decide which claim element ids the URL evidences. Match against both the decomposed elements AND the full claim. A page that describes the *same product feature* using different terminology than the claim is still a match (e.g. "recommendations" / "search suggestions" / "autocomplete" all map to elements about predicting and presenting likely items even if the claim says "incremental keystrokes" or "error model").
2. Assign a relevance score from 0.0 to 1.0.

Scoring:
- 1.0: Body or snippet directly describes product behaviour matching one or more claim limitations.
- 0.75: Strong evidence — describes the same feature using different vocabulary.
- 0.5: Adjacent/supporting evidence about a related product feature.
- 0.25: Weak contextual relevance — page mentions the feature area but does not describe the limitation directly.
- 0.0: Unrelated to the claim entirely.

Rules:
- When body text is present, weight it more heavily than the snippet — snippets are often generic SEO blurbs that understate relevance.
- Be associative, not literal. The claim uses patent jargon ("incremental keystrokes", "error model", "build string", "alphanumeric symbols"); product docs use feature names ("search", "autocomplete", "recommendations", "voice search", "library", "guide", "lineup"). These are the same thing.
- Use the patent description context (if provided) to identify the technical domain of the claim and prefer pages that fit that domain over pages that merely share surface-level vocabulary.
- Legal documents, terms of service, policies, and pricing pages are NOT documentation. Score them 0.0.
- Only assign 0.0 if the page is genuinely off-topic. Borderline pages in the right domain should score 0.25, not 0.0.
- Drop URLs that score 0.0 against every element.
- Prefer canonical documentation/help/answer pages over generic landing or per-content pages.
- Return JSON only.

Schema:
{{
  "ranked": [
    {{
      "url": "https://...",
      "score": 0.0,
      "matched_elements": ["E1", "E2"],
      "rationale": "short reason"
    }}
  ]
}}
"""


class RelevanceCheckingAgent:
    def __init__(
        self,
        llm: LLMClient,
        *,
        max_candidates_per_batch: int = 35,
        max_workers: int = 4,
    ) -> None:
        if max_candidates_per_batch < 1:
            raise ValueError("max_candidates_per_batch must be >= 1")
        self._llm = llm
        self.max_candidates_per_batch = max_candidates_per_batch
        self.max_workers = max(1, int(max_workers))

    def score(
        self,
        *,
        product: str,
        claim: str,
        elements: list[ClaimElement],
        hits: list[RawHit],
    ) -> list[ScoredURL]:
        if not hits:
            return []

        by_url, candidates = self._collect_candidates(hits)
        elements_payload = [
            {"id": e.id, "label": e.label, "keywords": e.keywords} for e in elements
        ]

        all_scored: list[ScoredURL] = []
        batches = list(chunked(candidates, self.max_candidates_per_batch))

        def _run(batch: list[dict[str, Any]]) -> list[Any]:
            return self._score_batch(
                product=product,
                claim=claim,
                elements_payload=elements_payload,
                batch=batch,
            )

        bar = progress(total=len(batches), desc="Agent2 scoring", unit="batch")
        try:
            if batches:
                workers = max(1, min(self.max_workers, len(batches)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(_run, b) for b in batches]
                    for future in as_completed(futures):
                        ranked = future.result()
                        for item in ranked:
                            scored = self._coerce_scored(item, by_url)
                            if scored is not None:
                                all_scored.append(scored)
                        bar.update(1)
        finally:
            bar.close()

        return self._dedupe(all_scored)

    @staticmethod
    def _collect_candidates(hits: list[RawHit]) -> tuple[dict[str, RawHit], list[dict[str, Any]]]:
        by_url: dict[str, RawHit] = {}
        surfaced_by: dict[str, set[str]] = {}
        for hit in hits:
            by_url.setdefault(hit.url, hit)
            surfaced_by.setdefault(hit.url, set()).add(hit.element_id)

        candidates: list[dict[str, Any]] = []
        for hit in by_url.values():
            entry: dict[str, Any] = {
                "url": hit.url,
                "title": hit.title,
                "snippet": hit.snippet,
                "domain": hit.domain,
                "surfaced_by_elements": sorted(surfaced_by[hit.url]),
            }
            if hit.body:
                entry["body"] = hit.body
            candidates.append(entry)
        return by_url, candidates

    def _score_batch(
        self,
        *,
        product: str,
        claim: str,
        elements_payload: list[dict[str, Any]],
        batch: list[dict[str, Any]],
    ) -> list[Any]:
        prompt = PROMPT_TEMPLATE.format(
            product=product,
            claim=claim.strip(),
            elements_json=json.dumps(elements_payload, indent=2),
            candidates_json=json.dumps(batch, indent=2),
        )
        try:
            text = self._llm.complete(
                system=SYSTEM_PROMPT,
                prompt=prompt,
                max_tokens=4000,
                temperature=0.0,
                json_mode=True,
            )
            data = parse_json_object(text)
        except Exception as exc:
            LOG.warning("Relevance batch scoring failed error=%s", exc)
            return []

        ranked = data.get("ranked")
        return ranked if isinstance(ranked, list) else []

    @staticmethod
    def _coerce_scored(item: Any, by_url: dict[str, RawHit]) -> ScoredURL | None:
        if not isinstance(item, dict):
            return None

        url = str(item.get("url") or "").strip()
        hit = by_url.get(url)
        if not hit:
            return None

        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        if score <= 0.0:
            return None

        matched_elements = item.get("matched_elements") or []
        if not isinstance(matched_elements, list):
            matched_elements = []
        clean_matched = [str(e).strip() for e in matched_elements if str(e).strip()]

        return ScoredURL(
            url=url,
            title=hit.title,
            snippet=hit.snippet,
            score=score,
            matched_elements=clean_matched,
            rationale=str(item.get("rationale") or "").strip(),
        )

    @staticmethod
    def _dedupe(all_scored: list[ScoredURL]) -> list[ScoredURL]:
        """When the same URL appears in multiple batches, keep the highest score.

        Ties merge ``matched_elements`` and concatenate rationales.
        """
        best: dict[str, ScoredURL] = {}
        for scored in all_scored:
            existing = best.get(scored.url)
            if existing is None or scored.score > existing.score:
                best[scored.url] = scored
                continue
            if scored.score == existing.score:
                existing.matched_elements = dedupe_keep_order(
                    existing.matched_elements + scored.matched_elements
                )
                if scored.rationale and scored.rationale not in existing.rationale:
                    merged = f"{existing.rationale}; {scored.rationale}".strip("; ")
                    existing.rationale = merged

        output = list(best.values())
        output.sort(key=lambda x: x.score, reverse=True)
        return output


__all__ = ["RelevanceCheckingAgent"]
