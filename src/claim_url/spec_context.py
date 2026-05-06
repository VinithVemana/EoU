"""Select patent description paragraphs relevant to a specific claim.

The patent specification uses concrete implementation vocabulary that claim
text abstracts away. Feeding relevant spec paragraphs to the rewriter and
extractor agents bridges the gap: the claim says "ordering items on a
display", the spec says "ranking content by predicted engagement score".

Paragraph selection has two modes:

- **keyword** (default, free): score paragraphs by term overlap with the
  claim text; top-N are returned in document order.
- **llm** (one extra LLM call): keyword pre-filter to ≤50 candidates, then
  the LLM picks the most semantically relevant ones.

Usage::

    from claim_url.pcs_api import fetch_patent_claim_and_description
    from claim_url.spec_context import SpecContext, build_spec_context

    claim_text, paragraphs = fetch_patent_claim_and_description(
        "US-10123456-B2", 1, api_key=..., base_url=..., port=...
    )
    ctx = build_spec_context(
        patent_number="US-10123456-B2",
        claim_number=1,
        claim_text=claim_text,
        paragraphs=paragraphs,
        max_paragraphs=10,
        llm=llm_client,   # optional; enables LLM-based selection
    )
    # embed in any agent prompt:
    spec_text = ctx.formatted(max_chars=2000)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from claim_url.llm import LLMClient


LOG = logging.getLogger("claim-url-finder")

_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "to", "and", "or", "is", "are", "be",
    "for", "with", "that", "this", "at", "on", "by", "as", "from", "it",
    "its", "not", "said", "wherein", "comprising", "configured", "based",
    "one", "more", "each", "any", "least", "having", "method", "system",
    "device", "computer", "processor", "memory", "data", "information",
    "step", "steps", "further", "also", "using", "used", "may", "can",
    "such", "than", "then", "when", "where", "which", "into", "between",
    "about", "after", "before", "during", "including", "includes", "include",
    "described", "shown", "figure", "embodiment", "present", "invention",
    "example", "according",
})

_LLM_SYSTEM = (
    "You select patent description paragraphs most relevant to a patent claim. "
    "Return valid JSON only."
)

_LLM_PROMPT = """\
Patent claim:
\"\"\"
{claim}
\"\"\"

Below are numbered paragraphs from the patent description (pre-filtered by
keyword overlap). Select the {max_n} most relevant that provide concrete
technical implementation detail for the claim limitations. Prefer paragraphs
describing HOW features work over boilerplate, prior-art summaries, or bare
figure captions.

Paragraphs (index → text):
{paragraphs_json}

Return JSON only:
{{
  "selected_indices": [0, 3, 7]
}}
"""


@dataclass(slots=True)
class SpecContext:
    """Relevant patent description paragraphs selected for a specific claim."""

    patent_number: str
    claim_number: int
    relevant_paragraphs: list[str] = field(default_factory=list)
    selection_method: str = "keyword"  # "keyword" | "llm"

    @property
    def relevant_text(self) -> str:
        return "\n\n".join(self.relevant_paragraphs)

    def formatted(self, max_chars: int = 2000) -> str:
        """Truncated text ready for injection into an LLM prompt."""
        text = self.relevant_text
        if not text:
            return ""
        if len(text) > max_chars:
            # break on paragraph boundary to avoid mid-sentence cuts
            text = text[:max_chars].rsplit("\n\n", 1)[0] + "\n[...truncated]"
        return text

    def __bool__(self) -> bool:
        return bool(self.relevant_paragraphs)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _claim_keywords(claim_text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z]{4,}", claim_text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _score_paragraph(para: str, keywords: set[str]) -> int:
    para_words = set(re.findall(r"[a-zA-Z]{4,}", para.lower()))
    return len(keywords & para_words)


def _select_by_keyword(
    paragraphs: list[str],
    claim_text: str,
    max_paragraphs: int,
) -> list[str]:
    if not paragraphs:
        return []
    keywords = _claim_keywords(claim_text)
    if not keywords:
        return paragraphs[:max_paragraphs]

    scored = [(i, _score_paragraph(p, keywords)) for i, p in enumerate(paragraphs)]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Restore document order among selected paragraphs for readability
    top_indices = sorted(idx for idx, sc in scored[:max_paragraphs] if sc > 0)
    if not top_indices:
        return paragraphs[:max_paragraphs]
    return [paragraphs[i] for i in top_indices]


def _select_by_llm(
    paragraphs: list[str],
    claim_text: str,
    max_paragraphs: int,
    llm: "LLMClient",
) -> list[str]:
    """Two-pass: keyword pre-filter to ≤50 candidates, then LLM picks best N."""
    from claim_url.utils import parse_json_object

    candidates = _select_by_keyword(paragraphs, claim_text, max_paragraphs=50)
    if len(candidates) <= max_paragraphs:
        return candidates

    paragraphs_json = json.dumps(
        {str(i): p for i, p in enumerate(candidates)}, indent=2
    )
    prompt = _LLM_PROMPT.format(
        claim=claim_text.strip(),
        max_n=max_paragraphs,
        paragraphs_json=paragraphs_json,
    )

    try:
        text = llm.complete(
            system=_LLM_SYSTEM,
            prompt=prompt,
            max_tokens=200,
            temperature=0.0,
            json_mode=True,
        )
        data = parse_json_object(text)
        indices = data.get("selected_indices", [])
        if not isinstance(indices, list):
            raise ValueError("selected_indices not a list")
        selected = sorted({
            int(i) for i in indices
            if isinstance(i, (int, float)) and 0 <= int(i) < len(candidates)
        })
        if selected:
            return [candidates[i] for i in selected]
    except Exception as exc:
        LOG.warning(
            "LLM spec-context selection failed, falling back to keyword: %s", exc
        )

    return candidates[:max_paragraphs]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_spec_context(
    patent_number: str,
    claim_number: int,
    claim_text: str,
    paragraphs: list[str],
    *,
    max_paragraphs: int = 10,
    llm: Optional["LLMClient"] = None,
) -> SpecContext:
    """Select relevant description paragraphs and return a :class:`SpecContext`.

    Args:
        patent_number:  Patent identifier, e.g. ``"US-10123456-B2"``.
        claim_number:   1-indexed claim number (stored for provenance).
        claim_text:     Plain-text of the claim (drives paragraph selection).
        paragraphs:     All description paragraphs from the patent, as returned
                        by :func:`~claim_url.pcs_api.fetch_patent_claim_and_description`.
        max_paragraphs: Maximum paragraphs to retain. Default: 10.
        llm:            Optional :class:`~claim_url.llm.LLMClient`. When
                        provided, enables semantic selection (one extra LLM
                        call). Without it, keyword overlap is used (free).

    Returns:
        :class:`SpecContext` with up to *max_paragraphs* selected paragraphs.
    """
    if not paragraphs:
        LOG.warning(
            "No description paragraphs available for patent '%s'", patent_number
        )
        return SpecContext(
            patent_number=patent_number,
            claim_number=claim_number,
            relevant_paragraphs=[],
            selection_method="keyword",
        )

    if llm is not None:
        selected = _select_by_llm(paragraphs, claim_text, max_paragraphs, llm)
        method = "llm"
    else:
        selected = _select_by_keyword(paragraphs, claim_text, max_paragraphs)
        method = "keyword"

    LOG.info(
        "Spec context: selected %d/%d paragraphs via %s (patent=%s claim=%d)",
        len(selected), len(paragraphs), method, patent_number, claim_number,
    )
    return SpecContext(
        patent_number=patent_number,
        claim_number=claim_number,
        relevant_paragraphs=selected,
        selection_method=method,
    )


__all__ = ["SpecContext", "build_spec_context"]
