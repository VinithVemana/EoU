"""Disk-backed JSON cache shared by SerpApi, LLM, and page-fetch layers.

Goal: skip every external call whose inputs we have already seen on
disk. Saves SerpApi credits, LLM tokens (and dollars), and HTTP round
trips. Cache entries are deterministic-only — callers must avoid storing
anything whose result depends on randomness (e.g. ``temperature > 0``).

Layout::

    <root>/<namespace>/<aa>/<full-sha256>.json

Each JSON file holds ``{"key": <input dict>, "value": <result>}``. The
``key`` field is kept for forensic inspection — collisions on a 256-bit
SHA are not a real concern, but having the original key makes the cache
debuggable (``grep`` for a query, etc.).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional


LOG = logging.getLogger("claim-url-finder")


class DiskCache:
    """Per-namespace JSON cache rooted at ``root``.

    Set ``enabled=False`` (or pass ``root=None``) to make every call a
    no-op — useful for ``--no-cache`` and for unit tests.
    """

    __slots__ = ("namespace", "enabled", "root", "_lock", "hits", "misses", "writes")

    def __init__(
        self,
        root: Optional[Path],
        namespace: str,
        *,
        enabled: bool = True,
    ) -> None:
        self.namespace = namespace
        self.enabled = bool(enabled and root is not None)
        self.root: Optional[Path] = (
            Path(root).expanduser() / namespace if self.enabled else None
        )
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.writes = 0

        if self.root is not None:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                LOG.warning("Cache disabled (cannot create %s): %s", self.root, exc)
                self.enabled = False
                self.root = None

    @staticmethod
    def _canonical(key: dict[str, Any]) -> str:
        return json.dumps(key, sort_keys=True, ensure_ascii=False, default=str)

    def _path(self, key: dict[str, Any]) -> Optional[Path]:
        if not self.enabled or self.root is None:
            return None
        h = hashlib.sha256(self._canonical(key).encode("utf-8")).hexdigest()
        return self.root / h[:2] / f"{h}.json"

    def get(self, key: dict[str, Any]) -> Optional[Any]:
        path = self._path(key)
        if path is None or not path.exists():
            with self._lock:
                self.misses += 1
            return None
        try:
            payload = json.loads(path.read_text("utf-8"))
            value = payload.get("value")
        except Exception as exc:
            LOG.debug("Cache read failed namespace=%s path=%s err=%s",
                      self.namespace, path, exc)
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        return value

    def set(self, key: dict[str, Any], value: Any) -> None:
        path = self._path(key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"key": key, "value": value},
                    ensure_ascii=False,
                    default=str,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception as exc:
            LOG.debug("Cache write failed namespace=%s path=%s err=%s",
                      self.namespace, path, exc)
            return
        with self._lock:
            self.writes += 1


__all__ = ["DiskCache"]
