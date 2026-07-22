"""Entry point for ``python -m coggym_check``."""

from __future__ import annotations

import sys

from coggym_check import cli


if __name__ == "__main__":
    sys.exit(cli.main())
