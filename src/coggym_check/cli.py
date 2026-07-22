"""Command-line interface for coggym_check.

Why stubs: this task (project scaffolding) only establishes the CLI surface
so later tasks can implement each pipeline stage (snapshot, lint, paper
extraction, materials download, etc.) independently without renegotiating
the entrypoint. Every subcommand below is a placeholder until its
corresponding task lands.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

#: One entry per pipeline-stage subcommand (see constraints.md's stage table).
SUBCOMMANDS = (
    "snapshot",
    "lint",
    "extract-paper",
    "download",
    "init-run",
    "validate-artifact",
    "render",
    "apply-fixes",
    "export-schemas",
    "run",
)


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level parser with one stub subparser per pipeline stage.

    A single shared implementation (rather than one function per subcommand)
    keeps the scaffold minimal until real argument signatures are known.
    """
    parser = argparse.ArgumentParser(
        prog="coggym_check",
        description=(
            "Validate CogGym experiment implementations against their "
            "source papers and author-released materials."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in SUBCOMMANDS:
        sub = subparsers.add_parser(name, help=f"{name} (not yet implemented)")
        sub.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch to the requested subcommand.

    Every subcommand is currently a stub: it reports itself as not
    implemented and exits with status 2, since none of the stage modules
    (schemas.py, dataset.py, lint.py, ...) exist yet.
    """
    parser = build_parser()
    parsed = parser.parse_args(argv)
    print(f"{parsed.command}: not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
