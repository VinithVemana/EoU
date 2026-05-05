"""Logging configuration for the CLI entrypoint.

Library callers should NOT call :func:`configure_logging` directly. Importing
the package attaches no handlers; the CLI configures stderr + file handlers
in one place so log output remains predictable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from claim_url.config import LOG_FORMAT


def configure_logging(
    *,
    console_level: int = logging.INFO,
    file_path: Optional[Path] = None,
    file_level: int = logging.DEBUG,
) -> Path:
    """Wire the root logger to a stderr console handler and a rotating file handler.

    Returns the resolved log file path.
    """
    import sys

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = file_path or Path("claim_url.log")
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    return log_path.resolve()


__all__ = ["configure_logging"]
