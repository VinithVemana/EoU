"""Compare two claim_url trace runs against a reference URL set.

Loads 07_final.json (required), 06_scoring.json, and 04_search.json (optional)
from each trace directory.  When the scoring / search layers are present, every
missed reference URL is classified by the pipeline stage that failed:

  top_k        — found in top-k output (hit)
  cutoff       — in scored pool at score >= 0.5 but below top-k rank cutoff
  score_low    — in scored pool but scored < 0.5 (relevance agent failure)
  not_scored   — URL reached search results but was never passed to Agent 2
  retrieval    — not in any search result (query generation failure)

Additional diagnostics (when 06_scoring.json is present):
  - Pool recall ceiling  — best possible recall using all scored URLs
  - Top-k projection     — recall at k=10,15,20,30,50,100,all-pool
  - Score-tier recall    — how many refs reachable at each score tier
  - Unique-output dedup  — flags multiple refs satisfied by one output URL
  - Match direction      — distinguishes exact / found-subpage / found-parent

Usage::

    # Compare two trace dirs against a newline-delimited reference file
    python scripts/eval_runs.py trace/run3 trace/run4 --refs refs.txt

    # Inline reference URLs
    python scripts/eval_runs.py trace/run3 trace/run4 \\
        --ref-url https://developers.google.com/maps/documentation/mobility \\
        --ref-url https://developers.google.com/maps/documentation/route-optimization/overview

    # JSON reference file (list of strings)
    python scripts/eval_runs.py trace/run3 trace/run4 --refs-json refs.json

    # JSON output (machine-readable, includes all stage data)
    python scripts/eval_runs.py trace/run3 trace/run4 --refs refs.txt --output json

    # Strict exact-URL matching (default: prefix)
    python scripts/eval_runs.py trace/run3 trace/run4 --refs refs.txt --match exact
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """All trace layers loaded from one run directory."""
    label: str
    urls: list[dict]          # 07_final.json → urls[]
    scored_pool: list[dict]   # 06_scoring.json → all_scored[] (empty if absent)
    search_hits: list[dict]   # 04_search.json  → kept_hits[] (empty if absent)

    @property
    def url_list(self) -> list[str]:
        return [u["url"] for u in self.urls]


@dataclass
class URLMatch:
    """Detailed match record for one reference URL against one run."""
    ref_url: str

    # Top-k result (primary hit)
    found: bool = False
    matched_url: str = ""
    score: float = 0.0
    rank: int = 0                  # 1-indexed position in top-k
    match_direction: str = ""      # "exact" | "child_of_ref" | "parent_of_ref"

    # Stage attribution for misses
    stage: str = "retrieval"       # "top_k"|"cutoff"|"score_low"|"not_scored"|"retrieval"

    # Closest URL found in scored pool (even when not top-k)
    pool_matched_url: str = ""
    pool_score: float = 0.0
    pool_rank: int = 0             # 1-indexed in full scored pool


@dataclass
class RunEval:
    label: str
    total_output: int
    hits: int
    pool_size: int
    pool_hits: int                 # refs reachable anywhere in scored pool
    matches: list[URLMatch] = field(default_factory=list)

    # Stage breakdown counts (for misses only)
    n_cutoff: int = 0
    n_score_low: int = 0
    n_not_scored: int = 0
    n_retrieval: int = 0

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

    @property
    def pool_recall(self) -> float:
        return self.pool_hits / len(self.matches) if self.matches else 0.0


# ---------------------------------------------------------------------------
# URL normalization & matching
# ---------------------------------------------------------------------------

def _normalize(url: str) -> str:
    u = urlparse(url.strip().lower())
    path = u.path.rstrip("/")
    return f"{u.scheme}://{u.netloc}{path}"


MatchMode = Literal["exact", "prefix", "domain"]


def _urls_match(candidate: str, reference: str, mode: MatchMode) -> bool:
    """Return True if candidate satisfies reference under the chosen mode.

    exact  — normalized URLs identical.
    prefix — either is a path-prefix of the other (subpages count).
    domain — candidate on same domain; reference path is prefix of candidate
             (candidate is the reference page or a subpage of it).
    """
    c = _normalize(candidate)
    r = _normalize(reference)
    if mode == "exact":
        return c == r
    if mode == "prefix":
        return c == r or c.startswith(r + "/") or r.startswith(c + "/")
    # domain mode
    cu, ru = urlparse(c), urlparse(r)
    if cu.netloc != ru.netloc:
        return False
    return cu.path == ru.path or cu.path.startswith(ru.path + "/")


def _match_direction(found_url: str, ref_url: str) -> str:
    """Characterise how the found URL relates to the reference URL."""
    f = _normalize(found_url)
    r = _normalize(ref_url)
    if f == r:
        return "exact"
    fp = urlparse(f).path.rstrip("/")
    rp = urlparse(r).path.rstrip("/")
    if fp.startswith(rp + "/"):
        return "child_of_ref"   # found a subpage — ref wants the overview
    if rp.startswith(fp + "/"):
        return "parent_of_ref"  # found a parent — ref wants a specific subpage
    return "prefix"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load_run(trace_dir: Path, label: str | None = None) -> RunResult:
    final = trace_dir / "07_final.json"
    if not final.exists():
        raise FileNotFoundError(f"07_final.json not found in {trace_dir}")
    data = json.loads(final.read_text())

    scored_pool: list[dict] = []
    scoring_path = trace_dir / "06_scoring.json"
    if scoring_path.exists():
        scored_pool = json.loads(scoring_path.read_text()).get("all_scored", [])

    search_hits: list[dict] = []
    search_path = trace_dir / "04_search.json"
    if search_path.exists():
        search_hits = json.loads(search_path.read_text()).get("kept_hits", [])

    return RunResult(
        label=label or trace_dir.name,
        urls=data.get("urls", []),
        scored_pool=scored_pool,
        search_hits=search_hits,
    )


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def _evaluate(run: RunResult, ref_urls: list[str], mode: MatchMode) -> RunEval:
    output_url_list = run.url_list
    scored_url_list = [u["url"] for u in run.scored_pool]
    search_url_list = [h["url"] for h in run.search_hits]

    matches: list[URLMatch] = []

    for ref in ref_urls:
        m = URLMatch(ref_url=ref)

        # --- Layer 1: top-k output ---
        for rank, out in enumerate(run.urls, start=1):
            if _urls_match(out["url"], ref, mode):
                m.found = True
                m.matched_url = out["url"]
                m.score = out.get("score", 0.0)
                m.rank = rank
                m.match_direction = _match_direction(out["url"], ref)
                m.stage = "top_k"
                break

        if m.found:
            matches.append(m)
            continue

        # --- Layer 2: scored pool ---
        if run.scored_pool:
            for pool_rank, out in enumerate(run.scored_pool, start=1):
                if _urls_match(out["url"], ref, mode):
                    m.pool_matched_url = out["url"]
                    m.pool_score = out.get("score", 0.0)
                    m.pool_rank = pool_rank
                    m.stage = "cutoff" if m.pool_score >= 0.5 else "score_low"
                    break

        # --- Layer 3: raw search results (reached SerpApi but not scored) ---
        if not m.pool_matched_url and run.search_hits:
            for hit in run.search_hits:
                if _urls_match(hit["url"], ref, mode):
                    m.pool_matched_url = hit["url"]
                    m.stage = "not_scored"
                    break

        # stage stays "retrieval" if nothing found in any layer
        matches.append(m)

    hits = sum(1 for m in matches if m.found)
    pool_hits = sum(
        1 for ref in ref_urls
        if any(_urls_match(u, ref, mode) for u in scored_url_list)
    ) if run.scored_pool else hits

    ev = RunEval(
        label=run.label,
        total_output=len(output_url_list),
        hits=hits,
        pool_size=len(run.scored_pool),
        pool_hits=pool_hits,
        matches=matches,
    )
    for m in matches:
        if not m.found:
            if m.stage == "cutoff":
                ev.n_cutoff += 1
            elif m.stage == "score_low":
                ev.n_score_low += 1
            elif m.stage == "not_scored":
                ev.n_not_scored += 1
            else:
                ev.n_retrieval += 1
    return ev


def compare(
    run_a: RunResult,
    run_b: RunResult,
    ref_urls: list[str],
    mode: MatchMode = "prefix",
) -> tuple[RunEval, RunEval]:
    return _evaluate(run_a, ref_urls, mode), _evaluate(run_b, ref_urls, mode)


# ---------------------------------------------------------------------------
# Top-k projection & score-tier analysis
# ---------------------------------------------------------------------------

def _topk_projection(
    run: RunResult, ref_urls: list[str], mode: MatchMode
) -> list[tuple[int, int]]:
    """Return [(k, hits_at_k)] for a range of k values."""
    if not run.scored_pool:
        return []
    pool_size = len(run.scored_pool)
    ks = sorted({10, 15, 20, 30, 50, 100, pool_size})
    results: list[tuple[int, int]] = []
    for k in ks:
        pool = [u["url"] for u in run.scored_pool[:k]]
        hits = sum(
            1 for r in ref_urls if any(_urls_match(u, r, mode) for u in pool)
        )
        results.append((k, hits))
    return results


def _score_tier_recall(
    run: RunResult, ref_urls: list[str], mode: MatchMode
) -> list[tuple[float, int, int]]:
    """Return [(threshold, urls_at_tier, refs_hit_at_tier)]."""
    if not run.scored_pool:
        return []
    results = []
    for threshold in [0.75, 0.50, 0.25, 0.0]:
        tier = [u["url"] for u in run.scored_pool if u.get("score", 0.0) >= threshold]
        hits = sum(1 for r in ref_urls if any(_urls_match(u, r, mode) for u in tier))
        results.append((threshold, len(tier), hits))
    return results


def _unique_output_map(ev: RunEval) -> dict[str, list[str]]:
    """Map each matched output URL → list of ref URLs it satisfies."""
    mapping: dict[str, list[str]] = {}
    for m in ev.matches:
        if m.found and m.matched_url:
            mapping.setdefault(m.matched_url, []).append(m.ref_url)
    return mapping


# ---------------------------------------------------------------------------
# Text output helpers
# ---------------------------------------------------------------------------

_W = 120


def _bar(val: float, width: int = 18) -> str:
    filled = round(val * width)
    return "█" * filled + "░" * (width - filled)


def _hdr(title: str) -> None:
    print(f"\n{'=' * _W}")
    print(f"  {title}")
    print("=" * _W)


def _stage_label(m: URLMatch) -> str:
    """Short cell text for the coverage table."""
    if m.found:
        direction = {"child_of_ref": "↓sub", "parent_of_ref": "↑par", "exact": ""}.get(
            m.match_direction, ""
        )
        suffix = f" {direction}" if direction else ""
        return f"HIT r{m.rank}[{m.score:.2f}]{suffix}"
    if m.stage == "cutoff":
        return f"CUTOFF r{m.pool_rank}[{m.pool_score:.2f}]"
    if m.stage == "score_low":
        return f"LOW r{m.pool_rank}[{m.pool_score:.2f}]"
    if m.stage == "not_scored":
        return "UNSCORE"
    return "MISS"


def print_comparison(
    run_a: RunResult,
    run_b: RunResult,
    eval_a: RunEval,
    eval_b: RunEval,
    ref_urls: list[str],
    mode: str,
) -> None:
    n_ref = len(ref_urls)
    has_pool = bool(run_a.scored_pool or run_b.scored_pool)

    _hdr(f"Run comparison: {eval_a.label!r} vs {eval_b.label!r}  (match={mode})")

    col = 46
    print(f"\n  {'Metric':<26} {eval_a.label:>{col}}   {eval_b.label:>{col}}")
    print(f"  {'-'*26} {'-'*col}   {'-'*col}")

    def row(name: str, a: str, b: str) -> None:
        print(f"  {name:<26} {a:>{col}}   {b:>{col}}")

    row("Output URLs",      str(eval_a.total_output), str(eval_b.total_output))
    row("Reference URLs",   str(n_ref),               str(n_ref))
    row("Hits (top-k)",
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

    if has_pool:
        row("Pool size (scored)",
            str(eval_a.pool_size) if eval_a.pool_size else "n/a",
            str(eval_b.pool_size) if eval_b.pool_size else "n/a")
        row("Pool recall ceiling",
            f"{eval_a.pool_recall:.1%}  {_bar(eval_a.pool_recall)}" if eval_a.pool_size else "n/a",
            f"{eval_b.pool_recall:.1%}  {_bar(eval_b.pool_recall)}" if eval_b.pool_size else "n/a")

    # Stage breakdown
    _hdr("Miss breakdown by pipeline stage")
    print(f"\n  {'Stage':<20} {'Meaning':<45} {eval_a.label:>18}   {eval_b.label:>18}")
    print(f"  {'-'*20} {'-'*45} {'-'*18}   {'-'*18}")

    def stage_row(name: str, meaning: str, a: int, b: int) -> None:
        print(f"  {name:<20} {meaning:<45} {a:>18}   {b:>18}")

    stage_row("retrieval",  "not in any search result — fix: queries",
              eval_a.n_retrieval, eval_b.n_retrieval)
    stage_row("score_low",  "found but scored <0.5 — fix: relevance prompt",
              eval_a.n_score_low, eval_b.n_score_low)
    stage_row("cutoff",     "score ≥0.5 but rank > top-k — fix: raise --top-k",
              eval_a.n_cutoff, eval_b.n_cutoff)
    stage_row("not_scored", "in search results, not passed to Agent 2",
              eval_a.n_not_scored, eval_b.n_not_scored)

    # Unique output dedup
    map_a = _unique_output_map(eval_a)
    map_b = _unique_output_map(eval_b)
    multi_a = {url: refs for url, refs in map_a.items() if len(refs) > 1}
    multi_b = {url: refs for url, refs in map_b.items() if len(refs) > 1}
    unique_a = len(map_a)
    unique_b = len(map_b)

    if eval_a.hits or eval_b.hits:
        _hdr("Hit quality — unique output URLs matched")
        print(f"\n  {eval_a.label}: {eval_a.hits} ref hits via {unique_a} unique output URL(s)")
        for url, refs in multi_a.items():
            print(f"    ⚠  {url}")
            for r in refs:
                print(f"       covers: {r}")
        print(f"\n  {eval_b.label}: {eval_b.hits} ref hits via {unique_b} unique output URL(s)")
        for url, refs in multi_b.items():
            print(f"    ⚠  {url}")
            for r in refs:
                print(f"       covers: {r}")

    # Per-reference coverage table
    _hdr("Reference URL coverage")
    ref_col = 72
    cell_col = 30
    print(f"\n  {'':2}{'#':<4} {'Reference URL':<{ref_col}} {eval_a.label:>{cell_col}}   {eval_b.label:>{cell_col}}")
    print(f"  {'':2}{'-'*4} {'-'*ref_col} {'-'*cell_col}   {'-'*cell_col}")

    for i, (ma, mb) in enumerate(zip(eval_a.matches, eval_b.matches), start=1):
        ref_short = ma.ref_url[:ref_col] if len(ma.ref_url) <= ref_col else ma.ref_url[:ref_col - 3] + "..."
        flag = "  "
        if ma.found and not mb.found:
            flag = "◀ "
        elif mb.found and not ma.found:
            flag = " ▶"
        elif not ma.found and not mb.found:
            flag = "✗✗"
        a_cell = _stage_label(ma)
        b_cell = _stage_label(mb)
        print(f"  {flag}{i:<4} {ref_short:<{ref_col}} {a_cell:>{cell_col}}   {b_cell:>{cell_col}}")

    # Top-k projection (only when scored pool is available)
    proj_a = _topk_projection(run_a, ref_urls, mode)  # type: ignore[arg-type]
    proj_b = _topk_projection(run_b, ref_urls, mode)  # type: ignore[arg-type]
    if proj_a or proj_b:
        _hdr("Top-k recall projection (scored pool)  ← shows if raising --top-k helps")
        print(f"\n  {'k':>6}  {eval_a.label:>20} {'recall':>8}   {eval_b.label:>20} {'recall':>8}")
        print(f"  {'-'*6}  {'-'*20} {'-'*8}   {'-'*20} {'-'*8}")
        for (ka, ha), (kb, hb) in zip(
            proj_a or [(0, 0)] * 7,
            proj_b or [(0, 0)] * 7,
        ):
            k = ka or kb
            ra_str = f"{ha}/{n_ref} ({ha/n_ref:.0%})" if ka else "n/a"
            rb_str = f"{hb}/{n_ref} ({hb/n_ref:.0%})" if kb else "n/a"
            marker = " ← current top-k" if k == (run_a.total_output if not proj_b else len(run_b.urls)) else ""
            print(f"  {k:>6}  {ra_str:>28}   {rb_str:>28}{marker}")

    # Score-tier recall
    tier_a = _score_tier_recall(run_a, ref_urls, mode)  # type: ignore[arg-type]
    tier_b = _score_tier_recall(run_b, ref_urls, mode)  # type: ignore[arg-type]
    if tier_a or tier_b:
        _hdr("Score-tier recall  ← shows relevance agent quality per tier")
        print(f"\n  {'Score ≥':>8}  {'URLs in tier':>14}  {'Refs hit':>10}  {'Recall':>8}   "
              f"{'URLs in tier':>14}  {'Refs hit':>10}  {'Recall':>8}")
        print(f"  {'':>8}  {'(' + eval_a.label + ')':>14}  {'':>10}  {'':>8}   "
              f"{'(' + eval_b.label + ')':>14}  {'':>10}  {'':>8}")
        print(f"  {'-'*8}  {'-'*14}  {'-'*10}  {'-'*8}   {'-'*14}  {'-'*10}  {'-'*8}")
        for (ta, urls_a, hits_a), (tb, urls_b, hits_b) in zip(
            tier_a or [(0.0, 0, 0)] * 4,
            tier_b or [(0.0, 0, 0)] * 4,
        ):
            t = ta or tb
            ra = f"{hits_a/n_ref:.0%}" if hits_a is not None else "n/a"
            rb = f"{hits_b/n_ref:.0%}" if hits_b is not None else "n/a"
            print(f"  {t:>8.2f}  {urls_a:>14}  {hits_a:>10}  {ra:>8}   "
                  f"{urls_b:>14}  {hits_b:>10}  {rb:>8}")

    # Unique/shared/missed sections
    only_a  = [m.ref_url for m, n in zip(eval_a.matches, eval_b.matches) if m.found and not n.found]
    only_b  = [m.ref_url for m, n in zip(eval_a.matches, eval_b.matches) if n.found and not m.found]
    both    = [m.ref_url for m, n in zip(eval_a.matches, eval_b.matches) if m.found and n.found]
    neither = [m.ref_url for m, n in zip(eval_a.matches, eval_b.matches) if not m.found and not n.found]

    def _section(title: str, items: list[str]) -> None:
        if not items:
            return
        _hdr(title)
        for url in items:
            print(f"  {url}")

    _section(f"Hits unique to {eval_a.label}", only_a)
    _section(f"Hits unique to {eval_b.label}", only_b)
    _section("Hits in BOTH runs", both)
    _section("Missed by BOTH runs (diagnose stage above)", neither)

    # Verdict
    print(f"\n  {'─'*_W}")
    print(f"  Verdict: ", end="")
    if eval_b.f1 > eval_a.f1 + 0.02:
        print(f"{eval_b.label} wins  (F1 +{eval_b.f1 - eval_a.f1:.1%})")
    elif eval_a.f1 > eval_b.f1 + 0.02:
        print(f"{eval_a.label} wins  (F1 +{eval_a.f1 - eval_b.f1:.1%})")
    else:
        print(f"Tie  (F1 {eval_a.label}={eval_a.f1:.1%}  {eval_b.label}={eval_b.f1:.1%})")

    if has_pool and neither:
        print(f"\n  {len(neither)} ref(s) missed by BOTH runs.")
        cutoff_recoverable = sum(
            1 for n in neither
            if any(
                (m.ref_url == n and m.stage in ("cutoff", "score_low"))
                for m in eval_b.matches
            )
        )
        if cutoff_recoverable:
            print(f"  {cutoff_recoverable} of those are in the scored pool — raise --top-k or --coverage-score-floor to recover.")
        retrieval_only = sum(
            1 for n in neither
            if all(
                m.ref_url != n or m.stage == "retrieval"
                for m in eval_b.matches
            )
        )
        if retrieval_only:
            print(f"  {retrieval_only} of those are retrieval misses — improve query coverage or sub-product probe.")
    print()


def print_json_output(eval_a: RunEval, eval_b: RunEval) -> None:
    def _ser(ev: RunEval) -> dict:
        return {
            "label": ev.label,
            "total_output": ev.total_output,
            "hits": ev.hits,
            "pool_size": ev.pool_size,
            "pool_hits": ev.pool_hits,
            "precision": round(ev.precision, 4),
            "recall": round(ev.recall, 4),
            "f1": round(ev.f1, 4),
            "pool_recall": round(ev.pool_recall, 4),
            "stage_breakdown": {
                "cutoff": ev.n_cutoff,
                "score_low": ev.n_score_low,
                "not_scored": ev.n_not_scored,
                "retrieval": ev.n_retrieval,
            },
            "matches": [asdict(m) for m in ev.matches],
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
        for line in refs_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    if refs_json:
        data = json.loads(refs_json.read_text())
        if isinstance(data, list):
            urls.extend(str(u) for u in data if u)
        elif isinstance(data, dict):
            urls.extend(str(u) for u in data.get("urls", []) if u)
    urls.extend(u for u in inline if u)
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
            "Requires 07_final.json; uses 06_scoring.json + 04_search.json "
            "for stage attribution when available."
        ),
    )
    p.add_argument("run_a", metavar="RUN_A", help="Path to first trace directory.")
    p.add_argument("run_b", metavar="RUN_B", help="Path to second trace directory.")

    refs = p.add_argument_group("reference URLs (at least one required)")
    refs.add_argument("--refs", metavar="FILE",
                      help="Newline-delimited text file of reference URLs (# comments ok).")
    refs.add_argument("--refs-json", metavar="FILE",
                      help="JSON file containing a list of reference URLs.")
    refs.add_argument("--ref-url", metavar="URL", action="append", dest="ref_urls", default=[],
                      help="Reference URL (repeatable).")

    p.add_argument("--label-a", default=None)
    p.add_argument("--label-b", default=None)
    p.add_argument("--match", choices=["exact", "prefix", "domain"], default="prefix",
                   help="URL matching mode. Default: prefix.")
    p.add_argument("--output", choices=["text", "json"], default="text")
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
        parser.error("At least one reference URL required (--refs / --refs-json / --ref-url).")

    try:
        run_a = _load_run(Path(args.run_a), args.label_a)
        run_b = _load_run(Path(args.run_b), args.label_b)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    mode: MatchMode = args.match  # type: ignore[assignment]
    eval_a, eval_b = compare(run_a, run_b, ref_urls, mode)

    if args.output == "json":
        print_json_output(eval_a, eval_b)
    else:
        print_comparison(run_a, run_b, eval_a, eval_b, ref_urls, mode)

    return 0


if __name__ == "__main__":
    sys.exit(main())
