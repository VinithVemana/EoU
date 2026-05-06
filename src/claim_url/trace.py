"""Per-stage JSON trace artifacts for post-run forensics.

When the CLI is invoked with ``--trace-dir DIR``, ``ClaimURLFinder``
writes one JSON file per pipeline stage so it is possible to inspect
exactly which queries fired, which URLs came back per (query, domain),
which bodies got fetched, and which scores Agent 2 assigned — without
re-running the pipeline.

Files emitted (numbered to preserve stage order on disk)::

    01_domains.json     selected domains + override flag
    02_elements.json    extracted ClaimElements
    03_queries.json     rewritten queries per element
    04_search.json      per-(query, domain) URL list + summary + raw hits
    05_pagefetch.json   {url: body_len} for each URL fetched
    06_scoring.json     all scored URLs *before* top-k cut
    07_final.json       FinderResult after top-k cut (top-k applied)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


LOG = logging.getLogger("claim-url-finder")


def _default(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Not JSON serializable: {type(obj).__name__}")


class TraceWriter:
    """Write numbered JSON files to ``root``. Caller decides payload shape."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, payload: Any) -> Path:
        path = self.root / name
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=_default)
        LOG.info("Trace: wrote %s", path)
        return path


__all__ = ["TraceWriter"]
