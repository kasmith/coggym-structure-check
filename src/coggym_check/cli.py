"""Command-line interface for coggym_check.

Why stubs remain for most subcommands: each pipeline stage (snapshot, lint,
paper extraction, materials download, etc.) is implemented in its own task
so the entrypoint doesn't need renegotiating each time. `validate-artifact`,
`export-schemas` (task 2), `snapshot` (task 3, dataset.py), `lint` (task 4,
lint.py), and `init-run` (task 5, rundir.py) are real here; every other
subcommand is still a placeholder until its corresponding task lands.

Why proper subparsers instead of the REMAINDER-stub pattern for these:
they have real, differing argument signatures (a positional path; an
optional --out; a positional study + optional --commit), which argparse
expresses more clearly as dedicated subparsers with a `func` callback than
by threading a generic REMAINDER list through ad hoc dispatch logic.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from coggym_check import config, dataset, lint, rundir, schemas

#: Pipeline-stage subcommands with no implementation yet (see
#: constraints.md's stage table). Each prints "not implemented" and exits 2.
STUB_SUBCOMMANDS = (
    "extract-paper",
    "download",
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


def _cmd_snapshot(args: argparse.Namespace) -> int:
    """Parse a study into a `StudySnapshot` and write `study_snapshot.json`.

    The snapshot is a pydantic model by construction (`dataset.load_study`
    returns one), so it is already validated; round-tripping it through
    `StudySnapshot.model_validate_json` on its own serialized output before
    writing is a cheap extra guard against any serialization-only drift
    (e.g. an `Any`-typed field encoding to something its own model would
    reject) rather than trusting construction-time validation alone.
    """
    try:
        snapshot = dataset.load_study(args.study, commit=args.commit)
    except dataset.StudyNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload = snapshot.model_dump_json(indent=2)
    schemas.StudySnapshot.model_validate_json(payload)

    run_dir = rundir.run_dir_for(snapshot.study, args.commit)
    (run_dir / "study_snapshot.json").write_text(payload)
    print(str(run_dir))
    return 0


#: Findings sort earlier the more severe they are, so the printed table
#: surfaces errors first regardless of check/experiment iteration order.
_LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}


def _print_findings_table(report: schemas.LintReport) -> None:
    """Print `report.findings` as a plain aligned table, errors first."""
    if not report.findings:
        print("no findings")
        return
    header = ("LEVEL", "CHECK", "EXPERIMENT", "MESSAGE")
    rows = [
        (f.level, f.check_id, f.experiment or "-", f.message)
        for f in sorted(report.findings, key=lambda f: (_LEVEL_ORDER[f.level], f.check_id))
    ]
    widths = [
        max(len(row[i]) for row in [header, *rows]) for i in range(len(header))
    ]

    def _fmt(row: tuple[str, str, str, str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(row, widths))

    print(_fmt(header))
    for row in rows:
        print(_fmt(row))


def _cmd_lint(args: argparse.Namespace) -> int:
    """Run structural lint over a study and write a validated `lint.json`.

    Exit codes: 1 if any `error`-level finding was produced, else 0 --
    mirrors `_cmd_snapshot`'s round-trip-validate-then-write pattern, and
    additionally prints a findings table (errors first) so a human running
    this directly sees the result without opening the JSON.
    """
    try:
        report = lint.lint_study(args.study, commit=args.commit)
    except dataset.StudyNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload = report.model_dump_json(indent=2)
    schemas.LintReport.model_validate_json(payload)

    run_dir = rundir.run_dir_for(report.study, args.commit)
    (run_dir / "lint.json").write_text(payload)

    _print_findings_table(report)
    print(str(run_dir))

    return 1 if any(f.level == "error" for f in report.findings) else 0


def _print_stage_status_table(statuses: dict[str, str]) -> None:
    """Print `stage_status`'s result as a plain aligned table, in pipeline order."""
    header = ("STAGE", "STATUS", "ARTIFACT")
    rows = [
        (stage, statuses[stage], rundir.STAGE_REGISTRY[stage].artifact)
        for stage in rundir.STAGE_REGISTRY
    ]
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]

    def _fmt(row: tuple[str, str, str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(row, widths))

    print(_fmt(header))
    for row in rows:
        print(_fmt(row))


def _cmd_init_run(args: argparse.Namespace) -> int:
    """Create (or reuse) a run dir + `run_meta.json`, then print its stage table.

    Idempotent per rundir.init_run: an existing `run_meta.json` is never
    overwritten, so re-running this against an in-progress run dir just
    reports current status.
    """
    run_dir = rundir.init_run(args.study, args.commit)
    _print_stage_status_table(rundir.stage_status(run_dir))
    print(str(run_dir))
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

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="Parse a study into a validated study_snapshot.json.",
    )
    snapshot_parser.add_argument("study", help="Study folder name under studies/.")
    snapshot_parser.add_argument(
        "--commit",
        default=None,
        help="Pin to a datasets-repo commit (reads via `git show`, never checks out).",
    )
    snapshot_parser.set_defaults(func=_cmd_snapshot)

    lint_parser = subparsers.add_parser(
        "lint",
        help="Run structural lint over a study and write a validated lint.json.",
    )
    lint_parser.add_argument("study", help="Study folder name under studies/.")
    lint_parser.add_argument(
        "--commit",
        default=None,
        help="Pin to a datasets-repo commit (reads via `git show`, never checks out).",
    )
    lint_parser.set_defaults(func=_cmd_lint)

    init_run_parser = subparsers.add_parser(
        "init-run",
        help="Create (or reuse) a run dir + run_meta.json and print its stage status table.",
    )
    init_run_parser.add_argument("study", help="Study folder name under studies/.")
    init_run_parser.add_argument(
        "--commit",
        default=None,
        help="Pin to a datasets-repo commit (run dir becomes runs/<study>@<shortsha>).",
    )
    init_run_parser.set_defaults(func=_cmd_init_run)

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
