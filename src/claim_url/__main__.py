"""Module entrypoint: ``python -m claim_url ...``."""

from __future__ import annotations

import sys

from claim_url.cli import main


if __name__ == "__main__":
    sys.exit(main())
