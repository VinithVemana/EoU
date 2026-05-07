"""Gradio UI for the claim URL finder pipeline."""

from __future__ import annotations

import argparse
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import gradio as gr

from claim_url import __version__
from claim_url.agents.domain import DomainIdentificationAgent
from claim_url.agents.product import ProductSuggestionAgent
from claim_url.cache import DiskCache
from claim_url.cli import _parse_domain_override, _parse_url_pattern_list
from claim_url.config import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_GOOGLE_MODEL,
    DEFAULT_LOG_FILE,
    DEFAULT_OPENAI_MODEL,
    ENV_PCS_API_KEY,
    ENV_PCS_BASE_URL,
    ENV_PCS_PORT,
    LLMProvider,
)
from claim_url.errors import ClaimURLError
from claim_url.fetch import PageFetcher
from claim_url.finder import ClaimURLFinder
from claim_url.llm import LLMClient
from claim_url.logging_setup import configure_logging
from claim_url.models import FinderResult
from claim_url.pcs_api import fetch_claim_from_patent, fetch_patent_claim_and_description
from claim_url.serp import SerpApiClient
from claim_url.spec_context import SpecContext, build_spec_context
from claim_url.trace import TraceWriter


LOG = logging.getLogger("claim-url-finder")

DEFAULT_EXCLUDE_PATTERNS = r"/browse/,/watch\?,/community-guide/"
DEFAULT_CLAIM_PATH = Path("claim.txt")


# --------------------------------------------------------------------------- #
# Research mode presets — applied to the visible controls when the mode radio
# changes. Users can still tweak any value after a preset is applied.
# --------------------------------------------------------------------------- #
RESEARCH_MODE_NORMAL = "Research"
RESEARCH_MODE_DEEP = "Deep Research"

# Order matches the change handler's `outputs` list in build_app().
RESEARCH_PRESETS: dict[str, dict[str, Any]] = {
    RESEARCH_MODE_NORMAL: {
        "top_k": 10,
        "queries_per_element": 4,
        "per_domain": 10,
        "max_subproducts": 8,
        "subproduct_two_step": False,
        "use_case_classification": True,
        "path_expansion": False,
        "path_expansion_max_followups": 12,
        "path_expansion_min_hits": 2,
        "index_link_harvest": True,
        "index_link_harvest_max_total": 200,
        "fetch_max_chars": 4000,
        "coverage_score_floor": 0.5,
        "coverage_score_floor_secondary": 0.25,
    },
    RESEARCH_MODE_DEEP: {
        # Wider top-k so element-coverage append doesn't push hits off the list.
        "top_k": 25,
        # 6 queries per element × 7 elements = 42 queries (vs 28 baseline).
        "queries_per_element": 6,
        # SerpApi pulls 15 results per (query, domain) instead of 10.
        "per_domain": 15,
        # 12 sub-product surfaces vs 8 — wider rewriter coverage.
        "max_subproducts": 12,
        # Two-step subproduct harvest: enumerate then filter (extra LLM call).
        "subproduct_two_step": True,
        "use_case_classification": True,
        # Path-neighborhood expansion ON with bigger budget + lower threshold.
        "path_expansion": True,
        "path_expansion_max_followups": 24,
        "path_expansion_min_hits": 1,
        # Index-page link harvest 2x the cap.
        "index_link_harvest": True,
        "index_link_harvest_max_total": 400,
        # Bigger fetch window so scorer sees more body text per page.
        "fetch_max_chars": 6000,
        "coverage_score_floor": 0.5,
        "coverage_score_floor_secondary": 0.25,
    },
}


CSS = """
.gradio-container {
  max-width: 100% !important;
}

#app-title {
  max-width: 1400px;
  margin: 0 auto 12px auto;
}

#app-title h1 {
  font-size: 28px;
  line-height: 1.15;
  margin-bottom: 4px;
}

#app-title p {
  color: var(--body-text-color-subdued);
  margin-top: 0;
}

#workspace {
  max-width: 1400px;
  margin: 0 auto;
}

.run-status {
  border: 1px solid var(--border-color-primary);
  border-left: 4px solid var(--primary-500);
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--background-fill-secondary);
  margin: 12px 0;
}

.run-status.running {
  border-left-color: var(--primary-500);
}

.run-status.done {
  border-left-color: #16a34a;
}

.run-status.error {
  border-left-color: #dc2626;
}

#cost-card {
  border: 1px solid var(--border-color-primary);
  border-radius: 10px;
  background: var(--background-fill-secondary);
  padding: 14px 16px;
  margin-top: 16px;
}

#settings-sidebar {
  min-width: 360px;
}

.compact-table textarea,
.compact-table input {
  font-size: 14px !important;
}

footer {
  display: none !important;
}
"""

THEME = gr.themes.Soft(
    primary_hue="blue",
    neutral_hue="slate",
    radius_size="sm",
    text_size="md",
)


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _default_claim() -> str:
    if DEFAULT_CLAIM_PATH.exists():
        return DEFAULT_CLAIM_PATH.read_text(encoding="utf-8")
    return ""

def _text(value: Any) -> str:
    """Return a safe string for optional Gradio textbox values."""
    if value is None:
        return ""
    return str(value)


def _optional_stripped(value: Any) -> Optional[str]:
    """Return stripped string or None."""
    value = _text(value).strip()
    return value or None


def _cache_root(cache_dir: Any, use_cache: bool) -> Optional[Path]:
    if not use_cache:
        return None

    value = _text(cache_dir).strip() or ".claim_url_cache"
    return Path(value).expanduser()


def _auto_trace_dir(base: str = "trace") -> Path:
    """Return ``<base>/run<N+1>`` where N is the highest existing ``runN``.

    Scans ``<base>/`` for sub-directories named ``run<int>`` and returns the
    next slot. ``trace/run1`` when no existing matches (or no base dir).
    """
    base_path = Path(base).expanduser()
    next_index = 1
    if base_path.is_dir():
        max_seen = 0
        for child in base_path.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if not name.startswith("run"):
                continue
            suffix = name[3:]
            if suffix.isdigit():
                max_seen = max(max_seen, int(suffix))
        next_index = max_seen + 1
    return base_path / f"run{next_index}"

def _read_claim(claim_text: str, claim_file: Optional[str]) -> str:
    """
    Read claim text.

    The textbox is preferred because uploaded files are copied into the textbox
    and the user may edit the text after upload.
    """
    if claim_text and claim_text.strip():
        return claim_text.strip()

    if claim_file:
        text = Path(claim_file).read_text(encoding="utf-8")
        if text.strip():
            return text.strip()

    raise ClaimURLError("Paste a claim or upload a claim text file.")


def _pcs_credentials(
    *,
    pcs_api_key: str,
    pcs_base_url: str,
    pcs_port: str,
) -> tuple[str, str, str]:
    """Resolve PCS credentials from UI inputs or the environment."""
    import os

    api_key = _text(pcs_api_key).strip() or os.environ.get(ENV_PCS_API_KEY, "")
    base_url = _text(pcs_base_url).strip() or os.environ.get(ENV_PCS_BASE_URL, "")
    port = _text(pcs_port).strip() or os.environ.get(ENV_PCS_PORT, "")

    missing = [
        name
        for name, val in [(ENV_PCS_API_KEY, api_key), (ENV_PCS_BASE_URL, base_url)]
        if not val
    ]
    if missing:
        raise ClaimURLError(
            f"PCS API credentials missing: {', '.join(missing)}. "
            "Set them in the Patent Lookup settings or your .env file."
        )

    return api_key, base_url, port


def load_claim_file_to_text(claim_file: Optional[str]) -> Any:
    """Populate the patent claim textbox when a claim file is uploaded."""
    if not claim_file:
        return gr.update()

    try:
        text = Path(claim_file).read_text(encoding="utf-8")
    except Exception as exc:
        LOG.exception("Failed to read uploaded claim file: %s", exc)
        raise gr.Error(f"Failed to read claim file: {exc}") from exc

    return text


def _normalise_model(provider: str, model: str) -> Optional[str]:
    model = _text(model).strip()
    if model:
        return model

    defaults = {
        LLMProvider.OPENAI.value: DEFAULT_OPENAI_MODEL,
        LLMProvider.CLAUDE.value: DEFAULT_CLAUDE_MODEL,
        LLMProvider.GOOGLE.value: DEFAULT_GOOGLE_MODEL,
    }
    return defaults.get(_text(provider))


def _build_llm(
    *,
    provider: str,
    model: str,
    llm_api_key: str,
    cache_root: Optional[Path],
    cache_enabled: bool,
) -> LLMClient:
    llm_cache = DiskCache(cache_root, "llm", enabled=cache_enabled)
    return LLMClient(
        provider=LLMProvider(provider),
        model=_normalise_model(provider, model),
        api_key=_optional_stripped(llm_api_key),
        cache=llm_cache,
    )


def _money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"${value:.4f}"


def _build_summary(
    *,
    result: FinderResult,
    llm: LLMClient,
    elapsed: float,
    serp_cache: DiskCache,
    fetch_cache: Optional[DiskCache],
    spec_context: Optional[SpecContext] = None,
) -> str:
    usage = llm.usage

    cost = _money(usage.cost_usd)
    saved_cost = _money(usage.cached_cost_usd)

    fetch_line = ""
    if fetch_cache is not None:
        fetch_line = (
            f"\nPage cache: {fetch_cache.hits} hits, "
            f"{fetch_cache.misses} misses, {fetch_cache.writes} writes"
        )

    spec_line = "Spec context: `not used`  \n"
    if spec_context:
        spec_line = (
            f"Spec context: `{len(spec_context.relevant_paragraphs)}` "
            f"description paragraphs via `{spec_context.selection_method}` selection  \n"
        )

    return (
        f"**Completed in {elapsed:.1f}s**\n\n"
        f"Product: `{result.product}`  \n"
        f"Domains: `{len(result.domains)}`  \n"
        f"Claim elements: `{len(result.elements)}`  \n"
        f"Ranked URLs: `{len(result.urls)}`  \n"
        f"{spec_line}\n"
        f"Provider: `{llm.provider.value}`  \n"
        f"Model: `{llm.model}`  \n"
        f"LLM calls: `{usage.calls}`  \n"
        f"Tokens: `{usage.total_tokens:,}` "
        f"(prompt `{usage.prompt_tokens:,}`, completion `{usage.completion_tokens:,}`)  \n"
        f"Estimated cost: `{cost}`\n\n"
        f"LLM cache: `{usage.cache_hits}` hits, `{usage.cached_total_tokens:,}` tokens saved, "
        f"`{saved_cost}` saved  \n"
        f"Serp cache: `{serp_cache.hits}` hits, `{serp_cache.misses}` misses, "
        f"`{serp_cache.writes}` writes"
        f"{fetch_line}"
    )


def _build_cost_panel(
    *,
    llm: LLMClient,
    elapsed: float,
    serp_cache: DiskCache,
    fetch_cache: Optional[DiskCache],
) -> str:
    usage = llm.usage

    page_cache_line = ""
    if fetch_cache is not None:
        page_cache_line = (
            f"| Page cache | {fetch_cache.hits} hits, "
            f"{fetch_cache.misses} misses, {fetch_cache.writes} writes |\n"
        )

    return f"""
### Session Cost

| Item | Value |
|---|---:|
| Estimated LLM cost | {_money(usage.cost_usd)} |
| Estimated cache savings | {_money(usage.cached_cost_usd)} |
| LLM calls | {usage.calls} |
| Total tokens | {usage.total_tokens:,} |
| Prompt tokens | {usage.prompt_tokens:,} |
| Completion tokens | {usage.completion_tokens:,} |
| Cached tokens saved | {usage.cached_total_tokens:,} |
| LLM cache | {usage.cache_hits} hits |
| Serp cache | {serp_cache.hits} hits, {serp_cache.misses} misses, {serp_cache.writes} writes |
{page_cache_line}
| Runtime | {elapsed:.1f}s |

> Cost is based on tracked LLM token usage. SerpApi subscription/API costs are not included.
"""


def _status_html(message: str, kind: str = "running") -> str:
    return f"<div class='run-status {kind}'>{message}</div>"


def _url_rows(result: FinderResult) -> list[list[Any]]:
    return [
        [
            f"{url.score:.2f}",
            url.title,
            ", ".join(url.matched_elements) or "-",
            url.url,
            url.rationale,
            url.snippet,
        ]
        for url in result.urls
    ]


def _domain_rows(result: FinderResult) -> list[list[Any]]:
    return [
        [
            domain.display(),
            f"{domain.confidence:.2f}",
            domain.rationale,
            "\n".join(domain.source_urls[:5]),
        ]
        for domain in result.domains
    ]


def _element_rows(result: FinderResult) -> list[list[Any]]:
    return [
        [
            element.id,
            element.label,
            ", ".join(element.keywords),
            "\n".join(element.queries(result.product)),
        ]
        for element in result.elements
    ]


def _empty_outputs(
    message: str,
    *,
    kind: str = "running",
    cost: str = "",
) -> tuple[str, list[Any], list[Any], list[Any], str, dict[str, Any], str]:
    return (
        _status_html(message, kind),
        [],
        [],
        [],
        "",
        {},
        cost,
    )


def load_claim_from_patent(
    patent_number: str,
    claim_number: int,
    pcs_api_key: str,
    pcs_base_url: str,
    pcs_port: str,
) -> tuple[Any, Any]:
    """Fetch claim text from PCS API and populate the claim textbox."""
    pn = (patent_number or "").strip()
    if not pn:
        raise gr.Error("Enter a patent number first.")

    try:
        api_key, base_url, port = _pcs_credentials(
            pcs_api_key=pcs_api_key,
            pcs_base_url=pcs_base_url,
            pcs_port=pcs_port,
        )
    except ClaimURLError as exc:
        raise gr.Error(str(exc)) from exc

    try:
        text = fetch_claim_from_patent(
            pn,
            int(claim_number),
            api_key=api_key,
            base_url=base_url,
            port=port,
        )
    except Exception as exc:
        raise gr.Error(f"Patent lookup failed: {exc}") from exc

    status_msg = f"Loaded claim {int(claim_number)} from **{pn}** ({len(text)} chars)."
    return text, gr.update(value=status_msg, visible=True)


def suggest_products(
    claim_text: str,
    claim_file: Optional[str],
    patent_number: str,
    claim_number: int,
    pcs_api_key: str,
    pcs_base_url: str,
    pcs_port: str,
    provider: str,
    model: str,
    llm_api_key: str,
    cache_dir: str,
    use_cache: bool,
    max_suggestions: int,
) -> tuple[Any, Any, Any]:
    try:
        try:
            claim = _read_claim(claim_text, claim_file)
        except ClaimURLError:
            pn = _text(patent_number).strip()
            if not pn:
                raise
            api_key, base_url, port = _pcs_credentials(
                pcs_api_key=pcs_api_key,
                pcs_base_url=pcs_base_url,
                pcs_port=pcs_port,
            )
            claim = fetch_claim_from_patent(
                pn,
                int(claim_number),
                api_key=api_key,
                base_url=base_url,
                port=port,
            )

        cache_root = _cache_root(cache_dir, use_cache)

        llm = _build_llm(
            provider=provider,
            model=model,
            llm_api_key=llm_api_key,
            cache_root=cache_root,
            cache_enabled=use_cache,
        )

        agent = ProductSuggestionAgent(llm=llm, max_suggestions=int(max_suggestions))
        suggestions = agent.suggest(claim)

    except Exception as exc:
        LOG.exception("Product suggestion failed: %s", exc)
        return (
            gr.update(),
            gr.update(value=[], visible=True),
            gr.update(value=f"Suggestion failed: {exc}", visible=True),
        )

    rows = [[item.name, item.vendor, item.rationale] for item in suggestions]

    # Fill the product textbox with the first suggestion if available.
    # The textbox remains fully editable, so users can type any custom product.
    product_value = rows[0][0] if rows else gr.update()

    if rows:
        status = (
            f"Found {len(rows)} product suggestion."
            if len(rows) == 1
            else f"Found {len(rows)} product suggestions."
        )
    else:
        status = "No product suggestions returned. You can type a custom product."

    return (
        product_value,
        gr.update(value=rows, visible=True),
        gr.update(value=status, visible=True),
    )


def discover_domains_for_review(
    product: str,
    provider: str,
    model: str,
    llm_api_key: str,
    serpapi_key: str,
    cache_dir: str,
    use_cache: bool,
    max_domains: int,
    domain_workers: int,
) -> tuple[Any, Any, Any]:
    """Run only Stage 1 and render a checklist for the user to review.

    Returns (checklist_update, status_update, state_update). The state stores
    the full ``DomainCandidate`` dicts keyed by spec so we can pass rationale
    + path_prefix back to the pipeline rather than only the host string.
    """
    if not product or not product.strip():
        return (
            gr.update(choices=[], value=[], visible=False),
            gr.update(value="Enter a product before discovering domains.", visible=True),
            {},
        )

    try:
        cache_root = _cache_root(cache_dir, use_cache)
        llm = _build_llm(
            provider=provider,
            model=model,
            llm_api_key=llm_api_key,
            cache_root=cache_root,
            cache_enabled=use_cache,
        )
        serp = SerpApiClient(
            api_key=_optional_stripped(serpapi_key),
            cache=DiskCache(cache_root, "serp", enabled=use_cache),
        )
        agent = DomainIdentificationAgent(
            llm=llm,
            serp=serp,
            max_domains=int(max_domains),
            max_workers=int(domain_workers),
        )
        domains = agent.discover(product.strip())
    except Exception as exc:
        LOG.exception("Domain discovery failed: %s", exc)
        return (
            gr.update(choices=[], value=[], visible=False),
            gr.update(value=f"Domain discovery failed: {exc}", visible=True),
            {},
        )

    if not domains:
        return (
            gr.update(choices=[], value=[], visible=False),
            gr.update(
                value="No domains discovered. Run Search will retry without review.",
                visible=True,
            ),
            {},
        )

    # CheckboxGroup choice values must be JSON-serialisable primitives —
    # use the display string and key the state dict by that same string so
    # selected values round-trip cleanly through the Gradio network layer.
    choices = [
        (f"{d.display()}  —  conf {d.confidence:.2f}", d.display())
        for d in domains
    ]
    values = [d.display() for d in domains]
    state = {d.display(): asdict(d) for d in domains}

    status = (
        f"Found {len(domains)} domain"
        f"{'s' if len(domains) != 1 else ''}. "
        "Uncheck any you want to skip, then click Run Search."
    )
    return (
        gr.update(choices=choices, value=values, visible=True),
        gr.update(value=status, visible=True),
        state,
    )


def _clear_domain_review() -> tuple[Any, Any, Any]:
    """Reset the review checklist + state when the product textbox changes."""
    return (
        gr.update(choices=[], value=[], visible=False),
        gr.update(value="", visible=False),
        {},
    )


def select_product_suggestion(rows: Any, evt: gr.SelectData) -> Any:
    """Copy clicked product suggestion into the editable product textbox."""
    if rows is None:
        return gr.update()

    try:
        row_index = evt.index[0] if isinstance(evt.index, (tuple, list)) else evt.index
        row = rows[int(row_index)]
        product_name = row[0]
    except Exception:
        return gr.update()

    return str(product_name)


def run_pipeline(
    claim_text: str,
    claim_file: Optional[str],
    patent_number: str,
    claim_number: int,
    spec_context_enabled: bool,
    max_spec_paragraphs: int,
    llm_spec_context: bool,
    pcs_api_key: str,
    pcs_base_url: str,
    pcs_port: str,
    product: str,
    domains: str,
    provider: str,
    model: str,
    serpapi_key: str,
    llm_api_key: str,
    top_k: int,
    max_domains: int,
    per_domain: int,
    queries_per_element: int,
    max_candidates_per_batch: int,
    fetch_pages: bool,
    fetch_max_chars: int,
    fetch_timeout: float,
    fetch_workers: int,
    domain_workers: int,
    search_workers: int,
    score_workers: int,
    exclude_url_patterns: str,
    cache_dir: str,
    use_cache: bool,
    trace_dir: str,
    save_trace: bool,
    subproduct_probe: bool,
    max_subproducts: int,
    subproduct_two_step: bool,
    use_case_classification: bool,
    path_expansion: bool,
    path_expansion_max_followups: int,
    path_expansion_min_hits: int,
    path_expansion_prefix_segments: int,
    index_link_harvest: bool,
    index_link_harvest_max_total: int,
    diversity_prefix_segments: int,
    diversity_per_prefix: int,
    element_coverage: bool,
    coverage_score_floor: float,
    coverage_score_floor_secondary: float,
    playwright_fetch: bool,
    fetch_adaptive_playwright: bool,
    selected_domain_specs: Optional[list[str]],
    domain_review_state: Optional[dict[str, Any]],
) -> Iterator[tuple[str, list[Any], list[Any], list[Any], str, dict[str, Any], str]]:
    started = time.time()
    page_fetcher: Optional[PageFetcher] = None

    yield _empty_outputs(
        "<strong>Preparing run…</strong><br/>Reading claim and validating settings.",
        cost="### Session Cost\n\nRun in progress.",
    )

    try:
        pn = _text(patent_number).strip()
        desc_paragraphs: list[str] = []

        try:
            claim = _read_claim(claim_text, claim_file)
        except ClaimURLError:
            if not pn:
                raise
            api_key, base_url, port = _pcs_credentials(
                pcs_api_key=pcs_api_key,
                pcs_base_url=pcs_base_url,
                pcs_port=pcs_port,
            )
            if spec_context_enabled:
                claim, desc_paragraphs = fetch_patent_claim_and_description(
                    pn,
                    int(claim_number),
                    api_key=api_key,
                    base_url=base_url,
                    port=port,
                )
            else:
                claim = fetch_claim_from_patent(
                    pn,
                    int(claim_number),
                    api_key=api_key,
                    base_url=base_url,
                    port=port,
                )

        product = (product or "").strip()
        if not product:
            raise ClaimURLError("Choose a suggested product or enter a custom product before running the search.")

        top_k = int(top_k)
        max_domains = int(max_domains)
        per_domain = int(per_domain)
        queries_per_element = int(queries_per_element)
        max_candidates_per_batch = int(max_candidates_per_batch)
        fetch_max_chars = int(fetch_max_chars)
        fetch_workers = int(fetch_workers)
        domain_workers = int(domain_workers)
        search_workers = int(search_workers)
        score_workers = int(score_workers)
        max_spec_paragraphs = int(max_spec_paragraphs)
        max_subproducts = int(max_subproducts)
        path_expansion_max_followups = int(path_expansion_max_followups)
        path_expansion_min_hits = int(path_expansion_min_hits)
        index_link_harvest_max_total = int(index_link_harvest_max_total)
        diversity_prefix_segments = int(diversity_prefix_segments)
        diversity_per_prefix = int(diversity_per_prefix)
        coverage_score_floor = float(coverage_score_floor)
        coverage_score_floor_secondary = float(coverage_score_floor_secondary)

        domain_override = _parse_domain_override(_text(domains))
        exclude_patterns: list[re.Pattern[str]] = _parse_url_pattern_list(_text(exclude_url_patterns))

        # If the user previewed and curated domains via the review checklist,
        # build full DomainCandidate objects from the cached state so we keep
        # rationale/confidence/path_prefix in the result. Empty selection
        # after discovery is treated as a user error (re-run discovery or
        # leave the field alone to fall through to live Stage 1).
        from claim_url.models import DomainCandidate as _DC
        preselected: Optional[list[_DC]] = None
        state = domain_review_state or {}
        selected = list(selected_domain_specs or [])
        if state:
            if not selected:
                raise ClaimURLError(
                    "You unchecked all reviewed domains. Re-run 'Discover Domains' "
                    "or check at least one before running the search."
                )
            preselected = []
            for spec in selected:
                cand_dict = state.get(spec)
                if not cand_dict:
                    continue
                preselected.append(_DC(
                    domain=cand_dict.get("domain", ""),
                    confidence=float(cand_dict.get("confidence", 1.0)),
                    rationale=cand_dict.get("rationale", "Selected via domain review"),
                    source_urls=list(cand_dict.get("source_urls") or []),
                    path_prefix=cand_dict.get("path_prefix"),
                ))
            if not preselected:
                raise ClaimURLError(
                    "Domain review state lost — re-run 'Discover Domains'."
                )
            # When preselected is set, the textbox override is redundant.
            domain_override = None

        cache_root = _cache_root(cache_dir, use_cache)
        llm_cache = DiskCache(cache_root, "llm", enabled=use_cache)
        serp_cache = DiskCache(cache_root, "serp", enabled=use_cache)
        fetch_cache = DiskCache(cache_root, "page", enabled=use_cache)

        yield _empty_outputs(
            "<strong>Connecting clients…</strong><br/>Initializing LLM, SerpApi, caches, and page fetcher.",
            cost="### Session Cost\n\nRun in progress.",
        )

        llm = LLMClient(
            provider=LLMProvider(provider),
            model=_normalise_model(provider, model),
            api_key=_optional_stripped(llm_api_key),
            cache=llm_cache,
        )

        serp = SerpApiClient(
            api_key=_optional_stripped(serpapi_key),
            cache=serp_cache,
        )
        

        if fetch_pages or playwright_fetch:
            import os
            from claim_url.config import ENV_FIRECRAWL_KEY
            firecrawl_key = os.environ.get(ENV_FIRECRAWL_KEY, "").strip() or None
            page_fetcher = PageFetcher(
                max_chars=fetch_max_chars,
                timeout=fetch_timeout,
                max_workers=fetch_workers,
                disk_cache=fetch_cache,
                use_playwright=bool(playwright_fetch),
                adaptive_playwright_fallback=bool(fetch_adaptive_playwright),
                firecrawl_api_key=firecrawl_key,
            )

        spec_context: Optional[SpecContext] = None
        if spec_context_enabled and pn:
            if not desc_paragraphs:
                api_key, base_url, port = _pcs_credentials(
                    pcs_api_key=pcs_api_key,
                    pcs_base_url=pcs_base_url,
                    pcs_port=pcs_port,
                )
                fetched_claim, desc_paragraphs = fetch_patent_claim_and_description(
                    pn,
                    int(claim_number),
                    api_key=api_key,
                    base_url=base_url,
                    port=port,
                )
                if not claim.strip():
                    claim = fetched_claim

            spec_context = build_spec_context(
                patent_number=pn,
                claim_number=int(claim_number),
                claim_text=claim,
                paragraphs=desc_paragraphs,
                max_paragraphs=max_spec_paragraphs,
                llm=llm if llm_spec_context else None,
            )

        trace_writer: Optional[TraceWriter] = None
        if save_trace:
            trace_value = _text(trace_dir).strip()
            if trace_value:
                trace_path = Path(trace_value).expanduser()
            else:
                trace_path = _auto_trace_dir()
            trace_writer = TraceWriter(trace_path)
            LOG.info("Trace dir: %s", trace_path)

        finder = ClaimURLFinder(
            llm=llm,
            serp=serp,
            max_domains=max_domains,
            per_domain=per_domain,
            max_candidates_per_batch=max_candidates_per_batch,
            queries_per_element=queries_per_element,
            exclude_url_patterns=exclude_patterns,
            page_fetcher=page_fetcher,
            domain_workers=domain_workers,
            search_workers=search_workers,
            score_workers=score_workers,
            trace_writer=trace_writer,
            enable_subproduct_probe=bool(subproduct_probe),
            max_subproducts=max_subproducts,
            subproduct_two_step_harvest=bool(subproduct_two_step),
            enable_use_case_classification=bool(use_case_classification),
            enable_path_expansion=bool(path_expansion),
            path_expansion_max_followups=path_expansion_max_followups,
            path_expansion_min_hits=path_expansion_min_hits,
            path_expansion_prefix_segments=path_expansion_prefix_segments,
            enable_index_link_harvest=bool(index_link_harvest),
            index_harvest_max_total_links=index_link_harvest_max_total,
            diversity_prefix_segments=diversity_prefix_segments,
            diversity_per_prefix=diversity_per_prefix,
            ensure_element_coverage=bool(element_coverage),
            coverage_score_floor=coverage_score_floor,
            coverage_score_floor_secondary=coverage_score_floor_secondary,
        )

        yield _empty_outputs(
            (
                "<strong>Finding evidence URLs…</strong><br/>"
                "This can take a while. The UI will update when the search completes."
            ),
            cost="### Session Cost\n\nRun in progress.",
        )

        result = finder.run(
            claim=claim,
            product=product,
            top_k=top_k,
            domain_override=domain_override,
            preselected_domains=preselected,
            spec_context=spec_context,
        )

        elapsed = time.time() - started

        status = _status_html(
            (
                f"<strong>Done.</strong> Found {len(result.urls)} ranked URLs for "
                f"<strong>{result.product}</strong> in {elapsed:.1f}s."
            ),
            "done",
        )

        summary = _build_summary(
            result=result,
            llm=llm,
            elapsed=elapsed,
            serp_cache=serp_cache,
            fetch_cache=fetch_cache if fetch_pages else None,
            spec_context=spec_context,
        )

        cost_panel = _build_cost_panel(
            llm=llm,
            elapsed=elapsed,
            serp_cache=serp_cache,
            fetch_cache=fetch_cache if fetch_pages else None,
        )

        yield (
            status,
            _url_rows(result),
            _domain_rows(result),
            _element_rows(result),
            summary,
            asdict(result),
            cost_panel,
        )

    except Exception as exc:
        LOG.exception("UI run failed: %s", exc)
        yield _empty_outputs(
            f"<strong>Run failed.</strong><br/>{exc}",
            kind="error",
            cost="### Session Cost\n\nRun failed before cost could be calculated.",
        )

    finally:
        if page_fetcher is not None:
            page_fetcher.close()


def build_app() -> gr.Blocks:
    with gr.Blocks(
        title="Claim URL Finder",
    ) as app:
        gr.Markdown(
            f"# Claim URL Finder\nPatent claim evidence discovery · v{__version__}",
            elem_id="app-title",
        )

        Sidebar = getattr(gr, "Sidebar", None)

        if Sidebar is not None:
            settings_panel = Sidebar(open=False, elem_id="settings-sidebar")
        else:
            settings_panel = gr.Accordion("Settings", open=False, elem_id="settings-sidebar")

        with settings_panel:
            gr.Markdown("## Settings")

            with gr.Accordion("LLM Provider", open=True):
                provider = gr.Radio(
                    label="Provider",
                    choices=[p.value for p in LLMProvider],
                    value=LLMProvider.OPENAI.value,
                )

                model = gr.Textbox(
                    label="Model",
                    value=DEFAULT_OPENAI_MODEL,
                    placeholder="Leave blank for provider default",
                )

                llm_api_key = gr.Textbox(
                    label="LLM API Key",
                    type="password",
                    placeholder="Uses provider env var when blank",
                )

            with gr.Accordion("Search Settings", open=False):
                serpapi_key = gr.Textbox(
                    label="SerpApi Key",
                    type="password",
                    placeholder="Uses SERPAPI_API_KEY when blank",
                )

                domains = gr.Textbox(
                    label="Domain Override",
                    placeholder="support.google.com,tv.youtube.com",
                )

                top_k = gr.Slider(
                    label="Top K",
                    minimum=1,
                    maximum=50,
                    value=10,
                    step=1,
                )

                max_domains = gr.Slider(
                    label="Max Domains",
                    minimum=1,
                    maximum=12,
                    value=3,
                    step=1,
                )

                per_domain = gr.Slider(
                    label="Results / Domain",
                    minimum=1,
                    maximum=25,
                    value=10,
                    step=1,
                )

                queries_per_element = gr.Slider(
                    label="Queries / Element",
                    minimum=1,
                    maximum=10,
                    value=4,
                    step=1,
                )

                max_candidates_per_batch = gr.Slider(
                    label="Candidates / Scoring Batch",
                    minimum=5,
                    maximum=75,
                    value=35,
                    step=1,
                )

                exclude_url_patterns = gr.Textbox(
                    label="Exclude URL Patterns",
                    value=DEFAULT_EXCLUDE_PATTERNS,
                )

            with gr.Accordion("Pipeline Features", open=False):
                subproduct_probe = gr.Checkbox(
                    label="Subproduct Probe",
                    value=True,
                )

                max_subproducts = gr.Slider(
                    label="Max Subproducts",
                    minimum=1,
                    maximum=20,
                    value=8,
                    step=1,
                )

                subproduct_two_step = gr.Checkbox(
                    label="Subproduct Two-Step Harvest (extra LLM call)",
                    value=False,
                )

                use_case_classification = gr.Checkbox(
                    label="Use-Case Classifier (extra LLM call)",
                    value=True,
                )

                path_expansion = gr.Checkbox(
                    label="Path-Neighborhood Expansion (extra SerpApi calls)",
                    value=False,
                )

                path_expansion_max_followups = gr.Slider(
                    label="Path-Expansion Max Follow-ups",
                    minimum=0,
                    maximum=48,
                    value=12,
                    step=2,
                )

                path_expansion_min_hits = gr.Slider(
                    label="Path-Expansion Min Hits / Prefix",
                    minimum=1,
                    maximum=10,
                    value=2,
                    step=1,
                )

                path_expansion_prefix_segments = gr.Slider(
                    label="Path-Expansion Prefix Segments",
                    minimum=1,
                    maximum=8,
                    value=3,
                    step=1,
                )

                index_link_harvest = gr.Checkbox(
                    label="Index-Page Link Harvest (no extra SerpApi cost)",
                    value=True,
                )

                index_link_harvest_max_total = gr.Slider(
                    label="Index-Harvest Max Total Links",
                    minimum=0,
                    maximum=800,
                    value=200,
                    step=50,
                )

                element_coverage = gr.Checkbox(
                    label="Element Coverage",
                    value=True,
                )

                coverage_score_floor = gr.Slider(
                    label="Coverage Score Floor (primary)",
                    minimum=0.0,
                    maximum=1.0,
                    value=0.5,
                    step=0.05,
                )

                coverage_score_floor_secondary = gr.Slider(
                    label="Coverage Score Floor (secondary fallback)",
                    minimum=0.0,
                    maximum=1.0,
                    value=0.25,
                    step=0.05,
                )

                diversity_prefix_segments = gr.Slider(
                    label="Diversity Prefix Segments",
                    minimum=1,
                    maximum=10,
                    value=4,
                    step=1,
                )

                diversity_per_prefix = gr.Slider(
                    label="Diversity URLs / Prefix",
                    minimum=1,
                    maximum=10,
                    value=3,
                    step=1,
                )

                save_trace = gr.Checkbox(
                    label="Save Trace (per-stage JSON artifacts)",
                    value=False,
                )

                trace_dir = gr.Textbox(
                    label="Trace Directory",
                    placeholder="trace/run<N+1>  (leave empty to auto-pick next slot)",
                    info="Leave empty when 'Save Trace' is on to auto-pick trace/runN+1.",
                )

            with gr.Accordion("Runtime", open=False):
                fetch_pages = gr.Checkbox(
                    label="Fetch Page Bodies",
                    value=True,
                )

                playwright_fetch = gr.Checkbox(
                    label="Playwright Fetch (Chromium, JS render + bot bypass)",
                    value=False,
                )

                fetch_adaptive_playwright = gr.Checkbox(
                    label="Adaptive Playwright Fallback (auto-promote bot-blocked hosts)",
                    value=True,
                )

                fetch_max_chars = gr.Slider(
                    label="Fetch Chars",
                    minimum=500,
                    maximum=12000,
                    value=4000,
                    step=500,
                )

                fetch_timeout = gr.Slider(
                    label="Fetch Timeout",
                    minimum=2,
                    maximum=30,
                    value=10,
                    step=1,
                )

                domain_workers = gr.Slider(
                    label="Domain Workers",
                    minimum=1,
                    maximum=16,
                    value=5,
                    step=1,
                )

                search_workers = gr.Slider(
                    label="Search Workers",
                    minimum=1,
                    maximum=32,
                    value=8,
                    step=1,
                )

                score_workers = gr.Slider(
                    label="Score Workers",
                    minimum=1,
                    maximum=16,
                    value=4,
                    step=1,
                )

                fetch_workers = gr.Slider(
                    label="Fetch Workers",
                    minimum=1,
                    maximum=32,
                    value=8,
                    step=1,
                )

            with gr.Accordion("Patent Lookup (PCS API)", open=False):
                spec_context_enabled = gr.Checkbox(
                    label="Use Spec Context",
                    value=True,
                )

                max_spec_paragraphs = gr.Slider(
                    label="Max Spec Paragraphs",
                    minimum=1,
                    maximum=30,
                    value=10,
                    step=1,
                )

                llm_spec_context = gr.Checkbox(
                    label="LLM Spec Selection",
                    value=False,
                )

                pcs_api_key = gr.Textbox(
                    label="PCS API Key",
                    type="password",
                    placeholder=f"Uses {ENV_PCS_API_KEY} env var when blank",
                )
                pcs_base_url = gr.Textbox(
                    label="PCS API Base URL",
                    placeholder=f"Uses {ENV_PCS_BASE_URL} env var when blank",
                )
                pcs_port = gr.Textbox(
                    label="PCS API Port",
                    placeholder=f"Uses {ENV_PCS_PORT} env var when blank",
                )

            with gr.Accordion("Cache", open=False):
                cache_dir = gr.Textbox(
                    label="Cache Directory",
                    value=".claim_url_cache",
                )

                use_cache = gr.Checkbox(
                    label="Use Disk Cache",
                    value=True,
                )

        with gr.Column(elem_id="workspace"):
            # Research mode preset — applies a bundle of slider/checkbox values
            # when toggled. User can still tweak any value afterwards.
            with gr.Row(equal_height=True):
                research_mode = gr.Radio(
                    label="Search Mode",
                    choices=[RESEARCH_MODE_NORMAL, RESEARCH_MODE_DEEP],
                    value=RESEARCH_MODE_NORMAL,
                    info=(
                        f"{RESEARCH_MODE_NORMAL}: lean default (fewer LLM/SerpApi "
                        f"calls, faster, ~$). {RESEARCH_MODE_DEEP}: maximises recall — "
                        "more queries, two-step subproduct, path expansion, larger "
                        "top-k, deeper index harvest. Costs ~2-3× normal."
                    ),
                )

            # Patent number lookup — populates the claim textbox automatically.
            with gr.Row(equal_height=True):
                with gr.Column(scale=5):
                    patent_number = gr.Textbox(
                        label="Patent Number",
                        placeholder="e.g. US-20120212660-A1",
                    )
                with gr.Column(scale=1):
                    claim_number_input = gr.Number(
                        label="Claim #",
                        value=1,
                        minimum=1,
                        step=1,
                        precision=0,
                    )
                with gr.Column(scale=2):
                    load_claim_button = gr.Button("Load Claim from Patent", variant="secondary")

            patent_load_status = gr.Markdown(visible=False)

            # Claim-file section: file uploader on the left, patent claim on the right.
            with gr.Row(equal_height=False):
                with gr.Column(scale=2):
                    claim_file = gr.File(
                        label="Claim File",
                        file_count="single",
                        file_types=[".txt"],
                        type="filepath",
                    )

                with gr.Column(scale=8):
                    claim_text = gr.Textbox(
                        label="Patent Claim",
                        value="",
                        lines=14,
                        max_lines=22,
                        placeholder="Paste claim text, upload a .txt file, or load from a patent number above...",
                    )

            # Product controls moved below the claim section.
            with gr.Row(equal_height=True):
                with gr.Column(scale=7):
                    product = gr.Textbox(
                        label="Product",
                        placeholder="Type a custom product or click a product suggestion below...",
                    )

                with gr.Column(scale=2):
                    max_suggestions = gr.Slider(
                        label="Number of Suggestions",
                        minimum=1,
                        maximum=12,
                        value=7,
                        step=1,
                    )

                with gr.Column(scale=3):
                    with gr.Row():
                        suggest_button = gr.Button(
                            "Suggest Products",
                            variant="secondary",
                        )

                        discover_button = gr.Button(
                            "Discover Domains",
                            variant="secondary",
                        )

                        run_button = gr.Button(
                            "Run Search",
                            variant="primary",
                        )

            suggestion_status = gr.Markdown(visible=False)

            # Domain review checklist — populated by Discover Domains. When
            # any items are selected here, Run Search uses them and skips
            # the pipeline's Stage 1 (saves SerpApi probes + an LLM call).
            domain_review_state = gr.State({})
            domain_review = gr.CheckboxGroup(
                label="Discovered Domains (uncheck to skip)",
                choices=[],
                value=[],
                visible=False,
                interactive=True,
            )
            domain_review_status = gr.Markdown(visible=False)

            # Product suggestions are not shown by default.
            # They become visible after clicking "Suggest Products".
            suggestions = gr.Dataframe(
                label="Product Suggestions",
                headers=["Product", "Vendor", "Rationale"],
                datatype=["str", "str", "str"],
                row_count=(0, "dynamic"),
                column_count=3,
                wrap=True,
                interactive=False,
                visible=False,
                type="array",
                elem_classes=["compact-table"],
            )

            status = gr.HTML(elem_id="run-status")

            with gr.Tabs():
                with gr.Tab("Ranked URLs"):
                    url_table = gr.Dataframe(
                        headers=["Score", "Title", "Elements", "URL", "Rationale", "Snippet"],
                        datatype=["str", "str", "str", "str", "str", "str"],
                        row_count=(0, "dynamic"),
                        column_count=6,
                        wrap=True,
                        interactive=False,
                        type="array",
                        elem_classes=["compact-table"],
                    )

                with gr.Tab("Domains"):
                    domain_table = gr.Dataframe(
                        headers=["Domain", "Confidence", "Rationale", "Sources"],
                        datatype=["str", "str", "str", "str"],
                        row_count=(0, "dynamic"),
                        column_count=4,
                        wrap=True,
                        interactive=False,
                        type="array",
                        elem_classes=["compact-table"],
                    )

                with gr.Tab("Claim Elements"):
                    element_table = gr.Dataframe(
                        headers=["ID", "Label", "Keywords", "Queries"],
                        datatype=["str", "str", "str", "str"],
                        row_count=(0, "dynamic"),
                        column_count=4,
                        wrap=True,
                        interactive=False,
                        type="array",
                        elem_classes=["compact-table"],
                    )

                with gr.Tab("Summary"):
                    summary = gr.Markdown()

                with gr.Tab("JSON"):
                    result_json = gr.JSON(label="Result")

            cost_panel = gr.Markdown(
                "### Session Cost\n\nNo run yet.",
                elem_id="cost-card",
            )

        load_claim_button.click(
            fn=load_claim_from_patent,
            inputs=[
                patent_number,
                claim_number_input,
                pcs_api_key,
                pcs_base_url,
                pcs_port,
            ],
            outputs=[claim_text, patent_load_status],
            show_progress="minimal",
        )

        provider.change(
            fn=lambda p: {
                LLMProvider.OPENAI.value: DEFAULT_OPENAI_MODEL,
                LLMProvider.CLAUDE.value: DEFAULT_CLAUDE_MODEL,
                LLMProvider.GOOGLE.value: DEFAULT_GOOGLE_MODEL,
            }.get(p, ""),
            inputs=provider,
            outputs=model,
        )

        # Research mode preset — when the radio toggles, push the preset values
        # into the relevant controls so the user sees what changed and can
        # still tweak anything before pressing Run.
        def _apply_research_preset(mode: str) -> tuple[Any, ...]:
            preset = RESEARCH_PRESETS.get(mode, RESEARCH_PRESETS[RESEARCH_MODE_NORMAL])
            return (
                gr.update(value=preset["top_k"]),
                gr.update(value=preset["queries_per_element"]),
                gr.update(value=preset["per_domain"]),
                gr.update(value=preset["max_subproducts"]),
                gr.update(value=preset["subproduct_two_step"]),
                gr.update(value=preset["use_case_classification"]),
                gr.update(value=preset["path_expansion"]),
                gr.update(value=preset["path_expansion_max_followups"]),
                gr.update(value=preset["path_expansion_min_hits"]),
                gr.update(value=preset["index_link_harvest"]),
                gr.update(value=preset["index_link_harvest_max_total"]),
                gr.update(value=preset["fetch_max_chars"]),
                gr.update(value=preset["coverage_score_floor"]),
                gr.update(value=preset["coverage_score_floor_secondary"]),
            )

        research_mode.change(
            fn=_apply_research_preset,
            inputs=research_mode,
            outputs=[
                top_k,
                queries_per_element,
                per_domain,
                max_subproducts,
                subproduct_two_step,
                use_case_classification,
                path_expansion,
                path_expansion_max_followups,
                path_expansion_min_hits,
                index_link_harvest,
                index_link_harvest_max_total,
                fetch_max_chars,
                coverage_score_floor,
                coverage_score_floor_secondary,
            ],
            show_progress="hidden",
        )

        claim_file.change(
            fn=load_claim_file_to_text,
            inputs=claim_file,
            outputs=claim_text,
            show_progress="minimal",
        )

        suggest_button.click(
            fn=suggest_products,
            inputs=[
                claim_text,
                claim_file,
                patent_number,
                claim_number_input,
                pcs_api_key,
                pcs_base_url,
                pcs_port,
                provider,
                model,
                llm_api_key,
                cache_dir,
                use_cache,
                max_suggestions,
            ],
            outputs=[
                product,
                suggestions,
                suggestion_status,
            ],
            show_progress="minimal",
        )

        suggestions.select(
            fn=select_product_suggestion,
            inputs=suggestions,
            outputs=product,
            show_progress="hidden",
        )

        discover_button.click(
            fn=discover_domains_for_review,
            inputs=[
                product,
                provider,
                model,
                llm_api_key,
                serpapi_key,
                cache_dir,
                use_cache,
                max_domains,
                domain_workers,
            ],
            outputs=[domain_review, domain_review_status, domain_review_state],
            show_progress="minimal",
        )

        # Clear stale review checklist when the product changes — domains
        # discovered for the previous product are no longer valid.
        product.change(
            fn=_clear_domain_review,
            inputs=None,
            outputs=[domain_review, domain_review_status, domain_review_state],
            show_progress="hidden",
        )

        run_button.click(
            fn=run_pipeline,
            inputs=[
                claim_text,
                claim_file,
                patent_number,
                claim_number_input,
                spec_context_enabled,
                max_spec_paragraphs,
                llm_spec_context,
                pcs_api_key,
                pcs_base_url,
                pcs_port,
                product,
                domains,
                provider,
                model,
                serpapi_key,
                llm_api_key,
                top_k,
                max_domains,
                per_domain,
                queries_per_element,
                max_candidates_per_batch,
                fetch_pages,
                fetch_max_chars,
                fetch_timeout,
                fetch_workers,
                domain_workers,
                search_workers,
                score_workers,
                exclude_url_patterns,
                cache_dir,
                use_cache,
                trace_dir,
                save_trace,
                subproduct_probe,
                max_subproducts,
                subproduct_two_step,
                use_case_classification,
                path_expansion,
                path_expansion_max_followups,
                path_expansion_min_hits,
                path_expansion_prefix_segments,
                index_link_harvest,
                index_link_harvest_max_total,
                diversity_prefix_segments,
                diversity_per_prefix,
                element_coverage,
                coverage_score_floor,
                coverage_score_floor_secondary,
                playwright_fetch,
                fetch_adaptive_playwright,
                domain_review,
                domain_review_state,
            ],
            outputs=[
                status,
                url_table,
                domain_table,
                element_table,
                summary,
                result_json,
                cost_panel,
            ],
            show_progress="hidden",
        )

    return app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the Claim URL Finder Gradio UI.")

    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Server host. Default: 127.0.0.1.",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=7860,
        help="Server port. Default: 7860.",
    )

    parser.add_argument(
        "--share",
        action="store_true",
        help="Create a public Gradio share URL.",
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console logging level. Default: INFO.",
    )

    parser.add_argument(
        "--log-file",
        default=None,
        help=f"Path to write the DEBUG-level log file. Default: ./{DEFAULT_LOG_FILE}",
    )

    return parser


def main(argv: Optional[list[str]] = None) -> None:
    _load_dotenv_if_available()

    args = build_arg_parser().parse_args(argv)

    log_path = Path(args.log_file) if args.log_file else Path(DEFAULT_LOG_FILE)

    configure_logging(
        console_level=getattr(logging, args.log_level.upper()),
        file_path=log_path,
    )

    LOG.info("Launching Claim URL Finder UI")

    build_app().queue().launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        theme=THEME,
        css=CSS,
    )


if __name__ == "__main__":
    main()
