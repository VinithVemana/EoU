"""Command-line interface for ``python -m claim_url`` / ``claim-url``.

Usage examples (always invoked via the venv pinned in the global
``CLAUDE.md`` — substitute the full Python path)::

    python -m claim_url --product "YouTube TV" --claim-file claim.txt
    # Defaults: max-domains=3, per-domain=10, queries-per-element=4, fetch-pages=on,
    #          exclude-url-patterns="/browse/,/watch\\?,/community-guide/", top-k=10
    python -m claim_url --product "YouTube TV" --claim-file claim.txt --no-fetch-pages
    python -m claim_url --llm claude --product "Netflix" --claim-file claim.txt --top-k 15
    python -m claim_url --llm google --product "Spotify" --claim "A computer-implemented..."
    python -m claim_url --claim-file claim.txt                    # no --product → LLM suggests products, user picks
    python -m claim_url --claim-file claim.txt --suggest-products 5  # cap suggestion list
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --domains "support.google.com,tv.youtube.com"
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --max-domains 3 --per-domain 3 --top-k 5                 # smoke test
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --queries-per-element 6 --per-domain 15                  # higher recall
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --queries-per-element 1 --no-fetch-pages                 # cheap
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --exclude-url-patterns ""                                # don't drop any URLs
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --search-workers 16 --score-workers 6                    # crank parallelism
    python -m claim_url --product X --claim-file c.txt --output json \\
        --log-level DEBUG --log-file /tmp/run.log
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --cache-dir .claim_url_cache                              # custom cache dir
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --no-cache                                                # disable cache
    python -m claim_url --product "Google Maps Platform" --claim-file claim_v2.txt  --trace-dir trace/run1
    python -m claim_url --product "Google Maps Platform" --claim-file claim_v2.txt \\
        --no-subproduct-probe                                     # skip sub-product probe
    python -m claim_url --product "Google Maps Platform" --claim-file claim_v2.txt \\
        --max-subproducts 12 --queries-per-element 6              # broader umbrella coverage
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --diversity-per-prefix 1 --diversity-prefix-segments 5    # strict path diversity
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --no-element-coverage                                     # plain top-k (no coverage append)
    python -m claim_url --product "YouTube TV" --claim-file claim.txt \\
        --coverage-score-floor 0.3                                # accept weaker covering hits
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from claim_url import __version__
from claim_url.agents.product import ProductSuggestion, ProductSuggestionAgent
from claim_url.cache import DiskCache
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
from claim_url.trace import TraceWriter
from claim_url.utils import dedupe_keep_order, normalize_domain


LOG = logging.getLogger("claim-url-finder")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claim-url",
        description="Find official product URLs relevant to patent claim elements using SerpApi.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    parser.add_argument(
        "--product",
        default=None,
        help=(
            "Product name, for example 'YouTube TV'. If omitted, the LLM "
            "suggests candidate products from the claim and you pick one "
            "interactively."
        ),
    )
    parser.add_argument(
        "--suggest-products", type=int, default=7,
        help=(
            "Max products to suggest when --product is omitted. "
            "Default: 7."
        ),
    )

    claim_group = parser.add_mutually_exclusive_group(required=True)
    claim_group.add_argument("--claim", help="Patent claim text.")
    claim_group.add_argument(
        "--claim-file", help="Path to a text file containing the patent claim."
    )

    parser.add_argument(
        "--llm",
        choices=[p.value for p in LLMProvider],
        default=LLMProvider.OPENAI.value,
        help="LLM provider. Default: openai.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Optional model override. Defaults: "
            f"openai={DEFAULT_OPENAI_MODEL}, "
            f"claude={DEFAULT_CLAUDE_MODEL}, "
            f"google={DEFAULT_GOOGLE_MODEL}"
        ),
    )

    parser.add_argument("--top-k", type=int, default=10, help="Ranked URLs to return. Default: 10.")
    parser.add_argument(
        "--max-domains", type=int, default=3, help="Max official domains Agent 1 may return. Default: 3."
    )
    parser.add_argument(
        "--per-domain", type=int, default=10,
        help="SerpApi results per claim element per domain. Default: 10.",
    )
    parser.add_argument(
        "--max-candidates-per-batch", type=int, default=35,
        help="Max URLs per LLM relevance-scoring batch. Default: 35.",
    )
    parser.add_argument(
        "--queries-per-element", type=int, default=4,
        help=(
            "Number of product-vocabulary queries QueryRewriteAgent generates "
            "per claim element. Higher = better recall, more SerpApi calls. "
            "Default: 4."
        ),
    )

    parser.add_argument(
        "--fetch-pages", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Fetch each candidate URL and pass the page body to Agent 2. "
            "Mirrors what websearch tools do internally; significantly improves "
            "recall when SerpApi snippets are generic. Adds N HTTP requests per "
            "run (N = unique candidate URLs). Default: on. Disable with "
            "--no-fetch-pages."
        ),
    )
    parser.add_argument(
        "--fetch-max-chars", type=int, default=4000,
        help="Max chars of stripped page text per URL when --fetch-pages is on. Default: 4000.",
    )
    parser.add_argument(
        "--fetch-timeout", type=float, default=10.0,
        help="HTTP timeout (seconds) per page fetch. Default: 10.",
    )
    parser.add_argument(
        "--fetch-workers", type=int, default=8,
        help="Parallel page-fetch workers. Default: 8.",
    )

    parser.add_argument(
        "--domain-workers", type=int, default=5,
        help="Parallel SerpApi probe workers for Agent 1 domain discovery. Default: 5.",
    )
    parser.add_argument(
        "--search-workers", type=int, default=8,
        help="Parallel SerpApi search workers for the (query, domain) plan. Default: 8.",
    )
    parser.add_argument(
        "--score-workers", type=int, default=4,
        help="Parallel LLM workers for Agent 2 relevance batches. Default: 4.",
    )

    parser.add_argument(
        "--exclude-url-patterns", default=r"/browse/,/watch\?,/community-guide/",
        help=(
            "Comma-separated regex patterns. Any candidate URL matching one of "
            "these is dropped before scoring. Useful to filter per-content "
            "landing pages. Patterns are matched with re.search. "
            r"Default: '/browse/,/watch\?,/community-guide/'. "
            "Pass '' to disable."
        ),
    )

    parser.add_argument(
        "--domains", default=None,
        help=(
            "Optional comma-separated domain override, e.g. "
            "'support.google.com,tv.youtube.com'. "
            "If provided, Agent 1 domain discovery is skipped."
        ),
    )

    parser.add_argument(
        "--serpapi-key", default=None,
        help="Optional SerpApi key. Defaults to SERPAPI_API_KEY env var.",
    )
    parser.add_argument(
        "--llm-api-key", default=None,
        help=(
            "Optional LLM API key override. Otherwise provider env var is used: "
            "OPENAI_API_KEY, ANTHROPIC_API_KEY, or GOOGLE_API_KEY."
        ),
    )

    parser.add_argument(
        "--cache-dir", default=".claim_url_cache",
        help=(
            "Directory for the disk cache (SerpApi, LLM, page bodies). "
            "Saves credits/tokens across runs by skipping calls whose "
            "inputs were already seen. Default: ./.claim_url_cache."
        ),
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Disable the disk cache for this run (every call hits the network).",
    )

    parser.add_argument(
        "--trace-dir", default=None,
        help=(
            "Optional directory to dump per-stage JSON artifacts: "
            "01_domains, 02_elements, 02b_subproducts, 03_queries, 04_search, "
            "05_pagefetch, 06_scoring, 07_final. Useful for forensics on why "
            "a URL was missed. Disabled by default."
        ),
    )

    parser.add_argument(
        "--subproduct-probe", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "Run a sub-product / feature-surface probe before query rewriting. "
            "Maps the claim's use-case onto specific sub-surfaces of the product "
            "(e.g. for a multi-API platform, picks the relevant APIs) and forces "
            "the rewriter to cover each. Default: on. Disable with "
            "--no-subproduct-probe."
        ),
    )
    parser.add_argument(
        "--max-subproducts", type=int, default=8,
        help="Cap on sub-product surfaces returned by the probe. Default: 8.",
    )
    parser.add_argument(
        "--diversity-prefix-segments", type=int, default=4,
        help=(
            "URL path segments used to bucket results for the tied-score "
            "diversity guard. Higher = stricter dedupe. Default: 4."
        ),
    )
    parser.add_argument(
        "--diversity-per-prefix", type=int, default=3,
        help=(
            "Max URLs per path-prefix bucket within a single tied-score tier. "
            "Excess URLs are pushed to the bottom of the tier so other prefixes "
            "get a chance in top-k. Default: 3."
        ),
    )
    parser.add_argument(
        "--element-coverage", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "After top-k selection, append one URL per claim element that has "
            "no representative in top-k (when a candidate scores >= "
            "--coverage-score-floor). Output may slightly exceed top-k. "
            "Default: on. Disable with --no-element-coverage."
        ),
    )
    parser.add_argument(
        "--coverage-score-floor", type=float, default=0.5,
        help=(
            "Minimum score a candidate URL must reach to qualify for the "
            "element-coverage guarantee. Default: 0.5."
        ),
    )

    parser.add_argument(
        "--output", choices=["text", "json"], default="text",
        help="Output format. Default: text.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console logging level. Default: INFO. File log is always DEBUG.",
    )
    parser.add_argument(
        "--log-file", default=None,
        help=f"Path to write the DEBUG-level log file. Default: ./{DEFAULT_LOG_FILE}",
    )

    return parser


def _read_claim(args: argparse.Namespace) -> str:
    if args.claim_file:
        return Path(args.claim_file).read_text(encoding="utf-8")
    if args.claim:
        return args.claim
    raise ValueError("Either --claim or --claim-file is required")


def _resolve_product(
    *,
    explicit: Optional[str],
    claim: str,
    llm: LLMClient,
    max_suggestions: int,
    stream=sys.stdout,
) -> str:
    """Return product name. If ``explicit`` empty, ask the LLM for suggestions and prompt user."""
    if explicit and explicit.strip():
        return explicit.strip()

    if not sys.stdin.isatty():
        raise ClaimURLError(
            "--product not provided and stdin is not a TTY; pass --product explicitly"
        )

    out = stream.write
    out("\n--product not provided. Asking the LLM for candidate products...\n")
    agent = ProductSuggestionAgent(llm=llm, max_suggestions=max_suggestions)
    try:
        suggestions = agent.suggest(claim)
    except Exception as exc:
        LOG.warning("Product suggestion failed: %s", exc)
        suggestions = []

    if suggestions:
        out("\n=== Suggested products ===\n")
        for idx, item in enumerate(suggestions, start=1):
            vendor = f" ({item.vendor})" if item.vendor else ""
            out(f"  [{idx}] {item.name}{vendor}\n")
            if item.rationale:
                out(f"      {item.rationale}\n")
        out("  [c] enter a custom product name\n")
    else:
        out("(no suggestions returned by the LLM)\n")

    while True:
        try:
            choice = input("\nPick a product [number / c / custom name]: ").strip()
        except EOFError:
            raise ClaimURLError("No product selected (EOF on stdin)") from None

        if not choice:
            continue

        if choice.isdigit() and suggestions:
            idx = int(choice)
            if 1 <= idx <= len(suggestions):
                picked = suggestions[idx - 1].name
                out(f"Using product: {picked}\n")
                return picked
            out(f"  invalid index — pick 1..{len(suggestions)} or type a custom name\n")
            continue

        if choice.lower() == "c":
            try:
                custom = input("Enter product name: ").strip()
            except EOFError:
                raise ClaimURLError("No product entered (EOF on stdin)") from None
            if custom:
                out(f"Using product: {custom}\n")
                return custom
            continue

        # Treat anything else as a literal product name.
        out(f"Using product: {choice}\n")
        return choice


def _parse_url_pattern_list(value: Optional[str]) -> list[re.Pattern[str]]:
    if not value:
        return []
    patterns: list[re.Pattern[str]] = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            patterns.append(re.compile(raw))
        except re.error as exc:
            raise ValueError(f"Invalid --exclude-url-patterns regex {raw!r}: {exc}") from exc
    return patterns


def _parse_domain_override(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None
    domains = [d for d in (normalize_domain(p) for p in value.split(",")) if d]
    domains = dedupe_keep_order(domains)
    if not domains:
        raise ValueError("--domains was provided but no valid domains were found")
    return domains


def _print_text_result(result: FinderResult, *, stream=sys.stdout) -> None:
    out = stream.write

    out("\n=== Product ===\n")
    out(f"{result.product}\n")

    out("\n=== Official domains identified ===\n")
    for domain in result.domains:
        out(f"  [{domain.confidence:.2f}] {domain.domain}\n")
        if domain.rationale:
            out(f"       {domain.rationale}\n")
        for source_url in domain.source_urls[:3]:
            out(f"       source: {source_url}\n")

    out("\n=== Claim elements ===\n")
    for element in result.elements:
        out(f"  {element.id}: {element.label}\n")
        out(f"       keywords: {', '.join(element.keywords)}\n")
        if element.search_queries:
            out(f"       queries: {' | '.join(element.search_queries)}\n")

    out("\n=== Ranked URLs ===\n")
    if not result.urls:
        out("  No relevant URLs found.\n")
        return

    for url in result.urls:
        matched = ", ".join(url.matched_elements) or "-"
        out(f"  [{url.score:.2f}] {url.title} ({matched})\n")
        out(f"       {url.url}\n")
        if url.rationale:
            out(f"       rationale: {url.rationale}\n")
        if url.snippet:
            out(f"       snippet: {url.snippet[:300]}\n")
        out("\n")


def _print_pricing_summary(
    llm: LLMClient,
    elapsed: float,
    *,
    serp_cache: Optional[DiskCache] = None,
    fetch_cache: Optional[DiskCache] = None,
    stream=sys.stdout,
) -> None:
    """Append model + token + cost + cache-savings summary to stdout and log."""
    usage = llm.usage
    cost_str = (
        f"${usage.cost_usd:.4f}" if usage.cost_usd is not None else "n/a (model not in pricing table)"
    )
    cached_cost_str = (
        f"${usage.cached_cost_usd:.4f}"
        if usage.cached_cost_usd is not None else "n/a"
    )

    out = stream.write
    out("\n=== Run summary ===\n")
    out(f"  provider:          {llm.provider.value}\n")
    out(f"  model:             {llm.model}\n")
    out(f"  llm calls:         {usage.calls}\n")
    out(f"  prompt tokens:     {usage.prompt_tokens:,}\n")
    out(f"  completion tokens: {usage.completion_tokens:,}\n")
    out(f"  total tokens:      {usage.total_tokens:,}\n")
    out(f"  estimated cost:    {cost_str}\n")
    out(f"  elapsed:           {elapsed:.1f}s\n")

    out("\n=== Cache savings ===\n")
    out(f"  llm cache hits:    {usage.cache_hits}\n")
    out(f"  tokens saved:      {usage.cached_total_tokens:,} "
        f"(prompt={usage.cached_prompt_tokens:,}, "
        f"completion={usage.cached_completion_tokens:,})\n")
    out(f"  cost saved:        {cached_cost_str}\n")
    if serp_cache is not None:
        out(f"  serp cache:        hits={serp_cache.hits} "
            f"misses={serp_cache.misses} writes={serp_cache.writes}\n")
    if fetch_cache is not None:
        out(f"  page cache:        hits={fetch_cache.hits} "
            f"misses={fetch_cache.misses} writes={fetch_cache.writes}\n")

    LOG.info(
        "Pricing summary: provider=%s model=%s calls=%d prompt=%d completion=%d "
        "total=%d cost=%s cache_hits=%d tokens_saved=%d cost_saved=%s",
        llm.provider.value,
        llm.model,
        usage.calls,
        usage.prompt_tokens,
        usage.completion_tokens,
        usage.total_tokens,
        cost_str,
        usage.cache_hits,
        usage.cached_total_tokens,
        cached_cost_str,
    )


def _load_dotenv_if_available() -> None:
    """Load ``.env`` from CWD or parents — only when invoked via the CLI."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def main(argv: Optional[list[str]] = None) -> int:
    _load_dotenv_if_available()

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    console_level = getattr(logging, args.log_level.upper())
    log_path = Path(args.log_file) if args.log_file else Path(DEFAULT_LOG_FILE)
    resolved = configure_logging(console_level=console_level, file_path=log_path)
    LOG.info("Logging to file: %s", resolved)

    started = time.time()

    try:
        claim = _read_claim(args)
        domain_override = _parse_domain_override(args.domains)
        exclude_patterns = _parse_url_pattern_list(args.exclude_url_patterns)

        cache_root: Optional[Path] = None
        if not args.no_cache:
            cache_root = Path(args.cache_dir).expanduser()
            LOG.info("Cache dir: %s", cache_root)
        else:
            LOG.info("Cache disabled (--no-cache)")

        llm_cache = DiskCache(cache_root, "llm", enabled=not args.no_cache)
        serp_cache = DiskCache(cache_root, "serp", enabled=not args.no_cache)
        fetch_cache = DiskCache(cache_root, "page", enabled=not args.no_cache)

        llm = LLMClient(
            provider=LLMProvider(args.llm),
            model=args.model,
            api_key=args.llm_api_key,
            cache=llm_cache,
        )
        serp = SerpApiClient(api_key=args.serpapi_key, cache=serp_cache)

        page_fetcher: Optional[PageFetcher] = None
        if args.fetch_pages:
            page_fetcher = PageFetcher(
                max_chars=args.fetch_max_chars,
                timeout=args.fetch_timeout,
                max_workers=args.fetch_workers,
                disk_cache=fetch_cache,
            )

        product = _resolve_product(
            explicit=args.product,
            claim=claim,
            llm=llm,
            max_suggestions=args.suggest_products,
        )

        trace_writer: Optional[TraceWriter] = None
        if args.trace_dir:
            trace_writer = TraceWriter(Path(args.trace_dir))
            LOG.info("Trace dir: %s", trace_writer.root)

        finder = ClaimURLFinder(
            llm=llm,
            serp=serp,
            max_domains=args.max_domains,
            per_domain=args.per_domain,
            max_candidates_per_batch=args.max_candidates_per_batch,
            queries_per_element=args.queries_per_element,
            exclude_url_patterns=exclude_patterns,
            page_fetcher=page_fetcher,
            domain_workers=args.domain_workers,
            search_workers=args.search_workers,
            score_workers=args.score_workers,
            trace_writer=trace_writer,
            enable_subproduct_probe=args.subproduct_probe,
            max_subproducts=args.max_subproducts,
            diversity_prefix_segments=args.diversity_prefix_segments,
            diversity_per_prefix=args.diversity_per_prefix,
            ensure_element_coverage=args.element_coverage,
            coverage_score_floor=args.coverage_score_floor,
        )

        try:
            result = finder.run(
                claim=claim,
                product=product,
                top_k=args.top_k,
                domain_override=domain_override,
            )
        finally:
            if page_fetcher is not None:
                page_fetcher.close()

        if args.output == "json":
            print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
        else:
            _print_text_result(result)

        elapsed = time.time() - started
        LOG.info(
            "Run summary: domains=%d elements=%d urls=%d elapsed=%.1fs",
            len(result.domains),
            len(result.elements),
            len(result.urls),
            elapsed,
        )
        _print_pricing_summary(
            llm,
            elapsed,
            serp_cache=serp_cache,
            fetch_cache=fetch_cache if args.fetch_pages else None,
        )
        return 0

    except KeyboardInterrupt:
        LOG.error("Interrupted")
        return 130
    except ClaimURLError as exc:
        LOG.error("Failed: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover - last-resort guard
        LOG.exception("Unexpected error: %s", exc)
        return 1


__all__ = ["build_arg_parser", "main"]
