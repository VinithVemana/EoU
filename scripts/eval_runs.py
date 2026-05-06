"""Compare two claim_url trace runs against a reference URL set.

Loads 07_final.json from each trace directory, matches output URLs against
a reference set, and prints precision / recall / F1 plus a per-URL coverage
table showing which run found each reference URL and at what score/rank.

Usage::

    # Compare two trace dirs against a newline-delimited reference file
    python scripts/eval_runs.py trace/run3 trace/run4 --refs refs.txt

    # Pass reference URLs directly on the command line
    python scripts/eval_runs.py trace/run3 trace/run4 \\
        --ref-url https://developers.google.com/maps/documentation/mobility \\
        --ref-url https://developers.google.com/maps/documentation/mobility/fleet-engine

    # JSON reference file (list of strings)
    python scripts/eval_runs.py trace/run3 trace/run4 --refs-json refs.json

    # Output as JSON (for scripting / CI)
    python scripts/eval_runs.py trace/run3 trace/run4 --refs refs.txt --output json

    # Adjust URL matching strictness (default: prefix)
    python scripts/eval_runs.py trace/run3 trace/run4 --refs refs.txt --match exact
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Loaded output from one trace run's 07_final.json."""
    label: str
    urls: list[dict]          # list of {url, score, title, matched_elements, ...}

    @property
    def url_list(self) -> list[str]:
        return [u["url"] for u in self.urls]


@dataclass
class URLMatch:
    """Whether a reference URL was found in a run's output."""
    ref_url: str
    found: bool = False
    matched_url: str = ""     # actual URL returned by the run
    score: float = 0.0
    rank: int = 0             # 1-indexed position in output list


@dataclass
class RunEval:
    label: str
    total_output: int
    hits: int
    matches: list[URLMatch] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.hits / self.total_output if self.total_output else 0.0

    @property
    def recall(self) -> float:
        return self.hits / len(self.matches) if self.matches else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


# ---------------------------------------------------------------------------
# URL normalization & matching
# ---------------------------------------------------------------------------

def _normalize(url: str) -> str:
    """Lowercase, strip trailing slash and fragment."""
    u = urlparse(url.strip().lower())
    path = u.path.rstrip("/")
    return f"{u.scheme}://{u.netloc}{path}"


def _urls_match(candidate: str, reference: str, mode: Literal["exact", "prefix", "domain"]) -> bool:
    """Return True if candidate matches reference under the chosen mode.

    - exact:  normalized URLs are identical.
    - prefix: either URL is a prefix of the other (handles subpages).
    - domain: candidate is on the same domain as reference and reference
              path is a prefix of candidate path (most lenient — useful
              when the pipeline finds deeper subpages of the reference).
    """
    c = _normalize(candidate)
    r = _normalize(reference)
    if mode == "exact":
        return c == r
    if mode == "prefix":
        return c == r or c.startswith(r + "/") or r.startswith(c + "/")
    # domain mode: candidate is on same domain, and reference path ⊆ candidate path
    cu, ru = urlparse(c), urlparse(r)
    if cu.netloc != ru.netloc:
        return False
    return cu.path == ru.path or cu.path.startswith(ru.path + "/")


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def _load_run(trace_dir: Path, label: str | None = None) -> RunResult:
    final = trace_dir / "07_final.json"
    if not final.exists():
        raise FileNotFoundError(f"07_final.json not found in {trace_dir}")
    data = json.loads(final.read_text())
    lbl = label or trace_dir.name
    return RunResult(label=lbl, urls=data.get("urls", []))


def _evaluate(
    run: RunResult,
    ref_urls: list[str],
    match_mode: Literal["exact", "prefix", "domain"],
) -> RunEval:
    output_urls = run.url_list
    matches: list[URLMatch] = []

    for ref in ref_urls:
        m = URLMatch(ref_url=ref)
        for rank, out in enumerate(run.urls, start=1):
            if _urls_match(out["url"], ref, match_mode):
                m.found = True
                m.matched_url = out["url"]
                m.score = out.get("score", 0.0)
                m.rank = rank
                break
        matches.append(m)

    hits = sum(1 for m in matches if m.found)
    return RunEval(
        label=run.label,
        total_output=len(output_urls),
        hits=hits,
        matches=matches,
    )


def compare(
    run_a: RunResult,
    run_b: RunResult,
    ref_urls: list[str],
    match_mode: Literal["exact", "prefix", "domain"] = "prefix",
) -> tuple[RunEval, RunEval]:
    return _evaluate(run_a, ref_urls, match_mode), _evaluate(run_b, ref_urls, match_mode)


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------

_W = 110  # total output width

def _bar(val: float, width: int = 20) -> str:
    filled = round(val * width)
    return "█" * filled + "░" * (width - filled)


def _header(title: str) -> None:
    print(f"\n{'=' * _W}")
    print(f"  {title}")
    print("=" * _W)


def print_comparison(
    eval_a: RunEval,
    eval_b: RunEval,
    ref_urls: list[str],
    match_mode: str,
) -> None:
    n_ref = len(ref_urls)
    _header(f"Run comparison: {eval_a.label!r} vs {eval_b.label!r}  (match={match_mode})")

    col = 42
    print(f"\n  {'Metric':<20} {'':>2} {eval_a.label:>{col}}   {eval_b.label:>{col}}")
    print(f"  {'-'*20} {'':>2} {'-'*col}   {'-'*col}")

    def row(name: str, a_val: str, b_val: str) -> None:
        print(f"  {name:<20} {'':>2} {a_val:>{col}}   {b_val:>{col}}")

    row("Output URLs",  str(eval_a.total_output), str(eval_b.total_output))
    row("Reference URLs", str(n_ref), str(n_ref))
    row("Hits",
        f"{eval_a.hits}/{n_ref}",
        f"{eval_b.hits}/{n_ref}")
    row("Recall",
        f"{eval_a.recall:.1%}  {_bar(eval_a.recall)}",
        f"{eval_b.recall:.1%}  {_bar(eval_b.recall)}")
    row("Precision",
        f"{eval_a.precision:.1%}  {_bar(eval_a.precision)}",
        f"{eval_b.precision:.1%}  {_bar(eval_b.precision)}")
    row("F1",
        f"{eval_a.f1:.1%}  {_bar(eval_a.f1)}",
        f"{eval_b.f1:.1%}  {_bar(eval_b.f1)}")

    # Per-reference coverage table
    _header("Reference URL coverage")
    col_a = 28
    col_b = 28
    ref_col = 70
    print(f"\n  {'#':<4} {'Reference URL':<{ref_col}} {eval_a.label:>{col_a}}   {eval_b.label:>{col_b}}")
    print(f"  {'-'*4} {'-'*ref_col} {'-'*col_a}   {'-'*col_b}")

    for i, (ma, mb) in enumerate(zip(eval_a.matches, eval_b.matches), start=1):
        ref_short = ma.ref_url
        if len(ref_short) > ref_col:
            ref_short = ref_short[:ref_col - 3] + "..."

        def cell(m: URLMatch) -> str:
            if m.found:
                return f"HIT r{m.rank} [{m.score:.2f}]"
            return "MISS"

        a_cell = cell(ma)
        b_cell = cell(mb)
        flag = "  "
        if ma.found and not mb.found:
            flag = "◀ "
        elif mb.found and not ma.found:
            flag = " ▶"
        elif ma.found and mb.found:
            flag = "  "
        else:
            flag = "✗✗"

        print(f"  {flag}{i:<2} {ref_short:<{ref_col}} {a_cell:>{col_a}}   {b_cell:>{col_b}}")

    # Runs-only sets
    only_a = [m.ref_url for m, n in zip(eval_a.matches, eval_b.matches) if m.found and not n.found]
    only_b = [m.ref_url for m, n in zip(eval_a.matches, eval_b.matches) if n.found and not m.found]
    both   = [m.ref_url for m, n in zip(eval_a.matches, eval_b.matches) if m.found and n.found]
    neither = [m.ref_url for m, n in zip(eval_a.matches, eval_b.matches) if not m.found and not n.found]

    def _section(title: str, urls: list[str]) -> None:
        if not urls:
            return
        _header(title)
        for url in urls:
            print(f"  {url}")

    _section(f"Hits unique to {eval_a.label} (not in {eval_b.label})", only_a)
    _section(f"Hits unique to {eval_b.label} (not in {eval_a.label})", only_b)
    _section("Hits in BOTH runs", both)
    _section("Missed by BOTH runs", neither)

    # Output URLs not in reference set
    ref_norm = [_normalize(r) for r in ref_urls]

    def _extra_urls(run: RunResult, eval_r: RunEval) -> list[str]:
        mode = match_mode  # type: ignore[arg-type]
        return [
            u["url"] for u in run.urls
            if not any(_urls_match(u["url"], r, mode) for r in ref_urls)  # type: ignore[arg-type]
        ]

    print()
    print(f"\n  {'─'*_W}")
    print(f"  Verdict: ", end="")
    if eval_b.f1 > eval_a.f1 + 0.05:
        delta = eval_b.f1 - eval_a.f1
        print(f"{eval_b.label} wins  (F1 +{delta:.1%} over {eval_a.label})")
    elif eval_a.f1 > eval_b.f1 + 0.05:
        delta = eval_a.f1 - eval_b.f1
        print(f"{eval_a.label} wins  (F1 +{delta:.1%} over {eval_b.label})")
    else:
        print(f"Tie  ({eval_a.label} F1={eval_a.f1:.1%}  {eval_b.label} F1={eval_b.f1:.1%})")
    print()


def print_json(eval_a: RunEval, eval_b: RunEval) -> None:
    def _ser(ev: RunEval) -> dict:
        return {
            "label": ev.label,
            "total_output": ev.total_output,
            "hits": ev.hits,
            "precision": round(ev.precision, 4),
            "recall": round(ev.recall, 4),
            "f1": round(ev.f1, 4),
            "matches": [
                {
                    "ref_url": m.ref_url,
                    "found": m.found,
                    "matched_url": m.matched_url,
                    "score": m.score,
                    "rank": m.rank,
                }
                for m in ev.matches
            ],
        }
    print(json.dumps({"run_a": _ser(eval_a), "run_b": _ser(eval_b)}, indent=2))


# ---------------------------------------------------------------------------
# Reference URL loading
# ---------------------------------------------------------------------------

def _load_refs(
    refs_file: Path | None,
    refs_json: Path | None,
    inline: list[str],
) -> list[str]:
    urls: list[str] = []

    if refs_file:
        lines = refs_file.read_text().splitlines()
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    if refs_json:
        data = json.loads(refs_json.read_text())
        if isinstance(data, list):
            urls.extend(str(u) for u in data if u)
        elif isinstance(data, dict):
            # accept {"urls": [...]} wrapper
            urls.extend(str(u) for u in data.get("urls", []) if u)

    urls.extend(u for u in inline if u)

    # Dedupe preserving order
    seen: set[str] = set()
    result: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval_runs",
        description=(
            "Compare two claim_url trace runs against a reference URL set. "
            "Reads 07_final.json from each trace directory."
        ),
    )
    p.add_argument("run_a", metavar="RUN_A", help="Path to first trace directory.")
    p.add_argument("run_b", metavar="RUN_B", help="Path to second trace directory.")

    ref_group = p.add_argument_group("reference URLs (at least one required)")
    ref_group.add_argument(
        "--refs", metavar="FILE",
        help="Path to a newline-delimited text file of reference URLs (# comments ok).",
    )
    ref_group.add_argument(
        "--refs-json", metavar="FILE",
        help="Path to a JSON file containing a list (or {\"urls\": [...]}) of reference URLs.",
    )
    ref_group.add_argument(
        "--ref-url", metavar="URL", action="append", dest="ref_urls", default=[],
        help="Reference URL (repeatable). May be combined with --refs / --refs-json.",
    )

    p.add_argument(
        "--label-a", default=None,
        help="Display label for RUN_A. Defaults to the directory name.",
    )
    p.add_argument(
        "--label-b", default=None,
        help="Display label for RUN_B. Defaults to the directory name.",
    )
    p.add_argument(
        "--match", choices=["exact", "prefix", "domain"], default="prefix",
        help=(
            "URL matching mode. "
            "exact: normalized URLs must be identical. "
            "prefix: either URL is a prefix of the other (default). "
            "domain: candidate on same domain with reference as path prefix."
        ),
    )
    p.add_argument(
        "--output", choices=["text", "json"], default="text",
        help="Output format. Default: text.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    ref_urls = _load_refs(
        Path(args.refs) if args.refs else None,
        Path(args.refs_json) if args.refs_json else None,
        args.ref_urls,
    )
    if not ref_urls:
        parser.error("At least one reference URL is required (--refs / --refs-json / --ref-url).")

    try:
        run_a = _load_run(Path(args.run_a), args.label_a)
        run_b = _load_run(Path(args.run_b), args.label_b)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    match_mode: Literal["exact", "prefix", "domain"] = args.match  # type: ignore[assignment]
    eval_a, eval_b = compare(run_a, run_b, ref_urls, match_mode)

    if args.output == "json":
        print_json(eval_a, eval_b)
    else:
        print_comparison(eval_a, eval_b, ref_urls, match_mode)

    return 0


if __name__ == "__main__":
    sys.exit(main())
