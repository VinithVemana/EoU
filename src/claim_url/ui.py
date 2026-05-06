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
from claim_url.agents.product import ProductSuggestionAgent
from claim_url.cache import DiskCache
from claim_url.cli import _parse_domain_override, _parse_url_pattern_list
from claim_url.config import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_GOOGLE_MODEL,
    DEFAULT_LOG_FILE,
    DEFAULT_OPENAI_MODEL,
    LLMProvider,
)
from claim_url.errors import ClaimURLError
from claim_url.fetch import PageFetcher
from claim_url.finder import ClaimURLFinder
from claim_url.llm import LLMClient
from claim_url.logging_setup import configure_logging
from claim_url.models import FinderResult
from claim_url.serp import SerpApiClient


LOG = logging.getLogger("claim-url-finder")

DEFAULT_EXCLUDE_PATTERNS = r"/browse/,/watch\?,/community-guide/"
DEFAULT_CLAIM_PATH = Path("claim.txt")


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

    return (
        f"**Completed in {elapsed:.1f}s**\n\n"
        f"Product: `{result.product}`  \n"
        f"Domains: `{len(result.domains)}`  \n"
        f"Claim elements: `{len(result.elements)}`  \n"
        f"Ranked URLs: `{len(result.urls)}`\n\n"
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
            domain.domain,
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


def suggest_products(
    claim_text: str,
    claim_file: Optional[str],
    provider: str,
    model: str,
    llm_api_key: str,
    cache_dir: str,
    use_cache: bool,
    max_suggestions: int,
) -> tuple[Any, Any, Any]:
    try:
        claim = _read_claim(claim_text, claim_file)
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
) -> Iterator[tuple[str, list[Any], list[Any], list[Any], str, dict[str, Any], str]]:
    started = time.time()
    page_fetcher: Optional[PageFetcher] = None

    yield _empty_outputs(
        "<strong>Preparing run…</strong><br/>Reading claim and validating settings.",
        cost="### Session Cost\n\nRun in progress.",
    )

    try:
        claim = _read_claim(claim_text, claim_file)

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

        domain_override = _parse_domain_override(_text(domains))
        exclude_patterns: list[re.Pattern[str]] = _parse_url_pattern_list(_text(exclude_url_patterns))

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
        

        if fetch_pages:
            page_fetcher = PageFetcher(
                max_chars=fetch_max_chars,
                timeout=fetch_timeout,
                max_workers=fetch_workers,
                disk_cache=fetch_cache,
            )

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
        theme=THEME,
        css=CSS,
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

            with gr.Accordion("Runtime", open=False):
                fetch_pages = gr.Checkbox(
                    label="Fetch Page Bodies",
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
                        placeholder="Paste claim text or upload a .txt claim file...",
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

                        run_button = gr.Button(
                            "Run Search",
                            variant="primary",
                        )

            suggestion_status = gr.Markdown(visible=False)

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

        provider.change(
            fn=lambda p: {
                LLMProvider.OPENAI.value: DEFAULT_OPENAI_MODEL,
                LLMProvider.CLAUDE.value: DEFAULT_CLAUDE_MODEL,
                LLMProvider.GOOGLE.value: DEFAULT_GOOGLE_MODEL,
            }.get(p, ""),
            inputs=provider,
            outputs=model,
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

        run_button.click(
            fn=run_pipeline,
            inputs=[
                claim_text,
                claim_file,
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
    )


if __name__ == "__main__":
    main()