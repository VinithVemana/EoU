"""Optional ``tqdm`` shim.

Importing :func:`progress` returns a real ``tqdm`` instance when ``tqdm``
is installed, otherwise a no-op pass-through that still supports the
small subset of the API the package uses (``set_postfix_str``,
``update``, ``close``, iteration).
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Optional

try:  # pragma: no cover - import guard
    from tqdm import tqdm as _tqdm

    _HAVE_TQDM = True
except ImportError:  # pragma: no cover
    _tqdm = None
    _HAVE_TQDM = False


class _NullProgress:
    """No-op replacement matching the subset of tqdm we use."""

    def __init__(self, iterable: Optional[Iterable[Any]] = None, **_: Any) -> None:
        self._iterable = iterable

    def __iter__(self) -> Iterator[Any]:
        return iter(self._iterable) if self._iterable is not None else iter(())

    def set_postfix_str(self, _: str) -> None:  # noqa: D401 - tqdm signature
        return None

    def update(self, _: int = 1) -> None:
        return None

    def close(self) -> None:
        return None


def progress(iterable: Optional[Iterable[Any]] = None, **kwargs: Any) -> Any:
    """Return a tqdm bar if available, else a silent no-op iterator/bar."""
    if _HAVE_TQDM:
        return _tqdm(iterable, **kwargs)  # type: ignore[misc]
    return _NullProgress(iterable, **kwargs)


__all__ = ["progress"]
