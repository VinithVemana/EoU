"""Shared pytest fixtures and helpers.

The ``src`` layout requires us to put ``src`` on ``sys.path`` so the
package is importable without installing it. ``pyproject.toml``'s
``[tool.pytest.ini_options]`` does this too, but adding it here keeps
``pytest tests/`` working when run from the repo root with no install.
"""

from __future__ import annotations

import sys
from pathlib import Path


_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
