"""Command-line interface for coggym_check.

Why stubs remain for most subcommands: each pipeline stage (snapshot, lint,
paper extraction, materials download, etc.) is implemented in its own task
so the entrypoint doesn't need renegotiating each time. `validate-artifact`
and `export-schemas` are real here (task 2, schemas.py); every other
subcommand is still a placeholder until its corresponding task lands.

Why proper subparsers instead of the REMAINDER-stub pattern for these two:
`validate-artifact` and `export-schemas` have real, differing argument
signatures (a positional path; an optional --out), which argparse expresses
more clearly as dedicated subparsers with a `func` callback than by
threading a generic REMAINDER list through ad hoc dispatch logic.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from coggym_check import config, schemas

#: Pipeline-stage subcommands with no implementation yet (see
#: constraints.md's stage table). Each prints "not implemented" and exits 2.
STUB_SUBCOMMANDS = (
    "snapshot",
    "lint",
    "extract-paper",
    "download",
    "init-run",
    "render",
    "apply-fixes",
    "run",
)


def _cmd_validate_artifact(args: argparse.Namespace) -> int:
    """Validate one artifact JSON file against the model its basename implies.

    Exit codes: 0 valid, 1 the file exists but fails to parse/validate,
    2 the basename isn't one of the known artifact filenames (a usage
    error, not a data error — distinguished so callers/scripts can tell
    "you validated the wrong path" apart from "the artifact is broken").
    """
    path = Path(args.path)
    basename = path.name
    model = schemas.FILENAME_MODEL_MAP.get(basename)
    if model is None:
        known = ", ".join(sorted(schemas.FILENAME_MODEL_MAP))
        print(
            f"unknown artifact filename '{basename}'; known artifact "
            f"filenames: {known}",
            file=sys.stderr,
        )
        return 2

    try:
        raw = path.read_text()
    except OSError as exc:
        print(f"{path}: cannot read file: {exc}", file=sys.stderr)
        return 1

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"{path}: invalid JSON: {exc}", file=sys.stderr)
        return 1

    try:
        model.model_validate(data)
    except ValidationError as exc:
        print(f"{path}: failed to validate as {model.__name__}:\n{exc}", file=sys.stderr)
        return 1

    print(f"{path}: valid {model.__name__}")
    return 0


def _cmd_export_schemas(args: argparse.Namespace) -> int:
    """Render every artifact model's JSON Schema + purpose into a Markdown doc."""
    out_path = Path(args.out) if args.out else config.repo_root() / "docs" / "artifacts.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(schemas.render_schemas_markdown())
    print(f"wrote {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level parser: stub subparsers for unimplemented
    pipeline stages, plus real subparsers for `validate-artifact` and
    `export-schemas`.
    """
    parser = argparse.ArgumentParser(
        prog="coggym_check",
        description=(
            "Validate CogGym experiment implementations against their "
            "source papers and author-released materials."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in STUB_SUBCOMMANDS:
        sub = subparsers.add_parser(name, help=f"{name} (not yet implemented)")
        sub.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    validate_parser = subparsers.add_parser(
        "validate-artifact",
        help="Validate a run artifact JSON file against its pydantic schema.",
    )
    validate_parser.add_argument(
        "path", help="Path to the artifact JSON file (basename determines the schema)."
    )
    validate_parser.set_defaults(func=_cmd_validate_artifact)

    export_parser = subparsers.add_parser(
        "export-schemas",
        help="Render every artifact model's JSON Schema + purpose into docs/artifacts.md.",
    )
    export_parser.add_argument(
        "--out",
        default=None,
        help="Output path (default: docs/artifacts.md under the repo root).",
    )
    export_parser.set_defaults(func=_cmd_export_schemas)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch to the requested subcommand.

    Subcommands with a `func` default (validate-artifact, export-schemas)
    run for real; everything else is still a stub that reports itself as
    not implemented and exits 2, since those stage modules don't exist yet.
    """
    parser = build_parser()
    parsed = parser.parse_args(argv)
    func = getattr(parsed, "func", None)
    if func is not None:
        return func(parsed)
    print(f"{parsed.command}: not implemented", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
