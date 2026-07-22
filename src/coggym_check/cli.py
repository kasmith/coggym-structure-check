"""Command-line interface for coggym_check.

Why stubs remain for most subcommands: each pipeline stage (snapshot, lint,
paper extraction, materials download, etc.) is implemented in its own task
so the entrypoint doesn't need renegotiating each time. `validate-artifact`,
`export-schemas` (task 2), `snapshot` (task 3, dataset.py), `lint` (task 4,
lint.py), `init-run` (task 5, rundir.py), `extract-paper` (task 6, paper.py),
`download` (task 7, materials.py), and `render`/`apply-fixes` (task 8,
report.py/fixer.py) are real here; only `run` (the headless full-pipeline
orchestrator, task 11) is still a placeholder.

Why proper subparsers instead of the REMAINDER-stub pattern for these:
they have real, differing argument signatures (a positional path; an
optional --out; a positional study + optional --commit), which argparse
expresses more clearly as dedicated subparsers with a `func` callback than
by threading a generic REMAINDER list through ad hoc dispatch logic.

`run` (task 11, headless.py) is the last of these: the full-pipeline
headless batch orchestrator, taking either a single study or a
`--studies-from` file of study names, run sequentially.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, ValidationError

from coggym_check import (
    config,
    dataset,
    fixer,
    headless,
    lint,
    materials,
    paper,
    report,
    rundir,
    schemas,
)

#: Pipeline-stage subcommands with no implementation yet (see
#: constraints.md's stage table). Each prints "not implemented" and exits 2.
#: Empty now that task 11 (`run`, headless.py) is implemented — kept as the
#: harness for whatever the next new subcommand turns out to be.
STUB_SUBCOMMANDS: tuple[str, ...] = ()


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


def _cmd_extract_paper(args: argparse.Namespace) -> int:
    """Locate+extract a study's paper.pdf and write a validated `paper_status.json`.

    `paper.pdf` is always read from the datasets repo's working tree (see
    `paper.py`'s module docstring): `--commit` here only selects which run
    dir (`runs/<study>` vs `runs/<study>@<shortsha>`) the artifact lands in,
    it does not pin which PDF gets read.

    Exit codes: 0 if `paper.pdf` was found and extracted (`status ==
    "found_local"`), 1 if it wasn't (`status == "not_found"`) -- mirroring
    `_cmd_lint`'s "1 means something downstream still needs attention"
    convention, here signaling that the `paper-finder` agent should run
    before later stages proceed. Either outcome is a normal, valid artifact
    write, not a usage error.
    """
    status = paper.extract_paper(args.study)

    payload = status.model_dump_json(indent=2)
    schemas.PaperStatus.model_validate_json(payload)

    run_dir = rundir.run_dir_for(status.study, args.commit)
    (run_dir / "paper_status.json").write_text(payload)

    print(f"status: {status.status}")
    if status.text_quality is not None:
        print(f"text_quality: {status.text_quality:.3f}")
    print(str(run_dir))

    return 0 if status.status == "found_local" else 1


def _cmd_download(args: argparse.Namespace) -> int:
    """Download every `pending` source in a study's `materials_manifest.json`.

    The manifest itself is written by the `materials-scout` agent (Stage 2)
    into the run dir; this subcommand only locates it there, downloads
    pending sources (materials.download_manifest), and rewrites the file
    with `local_path`/`sha256_or_commit`/`download_status` back-filled.

    Exit codes (task-7 brief): 0 if at least one previously-pending source
    ended up `ok`, or if there were no pending sources to begin with; 1 if
    there were pending sources and none of them ended up `ok` (whether they
    failed outright or were skipped as too large). 1 also if the manifest
    itself isn't there yet -- a usage/ordering error, not a data error, but
    exit 1 is the simplest signal that this run dir isn't ready for
    `download` yet.
    """
    run_dir = rundir.run_dir_for(args.study, args.commit)
    manifest_path = run_dir / "materials_manifest.json"
    if not manifest_path.exists():
        print(
            f"{manifest_path}: not found; run the materials-scout agent first",
            file=sys.stderr,
        )
        return 1

    manifest = schemas.MaterialsManifest.model_validate_json(manifest_path.read_text())
    n_pending = sum(1 for s in manifest.sources if s.download_status == "pending")

    updated = materials.download_manifest(manifest)

    payload = updated.model_dump_json(indent=2)
    schemas.MaterialsManifest.model_validate_json(payload)
    manifest_path.write_text(payload)

    ok_count = sum(
        1
        for orig, new in zip(manifest.sources, updated.sources)
        if orig.download_status == "pending" and new.download_status == "ok"
    )

    if n_pending == 0:
        print("no pending sources")
    else:
        print(f"{ok_count}/{n_pending} pending sources downloaded ok")
    print(str(run_dir))

    return 0 if (n_pending == 0 or ok_count > 0) else 1


def _now_iso() -> str:
    """ISO-8601 UTC timestamp, matching dataset.py's/rundir.py's `_now_iso`
    format -- kept as a third private copy rather than a shared import
    since it's one line and each module already has its own."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_optional_artifact(path: Path, model: type[BaseModel]) -> BaseModel | None:
    """Read+validate `path` against `model`, or `None` if `path` doesn't
    exist -- the "degrade gracefully" half of `render`'s contract (task-8
    brief): `fix_plan.json`/`paper_status.json`/`materials_manifest.json`
    are each optional inputs to a render."""
    if not path.exists():
        return None
    return model.model_validate_json(path.read_text())


def _cmd_render(args: argparse.Namespace) -> int:
    """Render a study's `comparison.json` (+ whichever of `fix_plan.json`/
    `paper_status.json`/`materials_manifest.json` exist) into `report.md` +
    `pr.md` in its run dir.

    Exit codes: 1 if `comparison.json` itself is missing (the one required
    input -- report.py's `render()` has nothing to render without it), else
    0. Missing optional artifacts are not an error (`report.py` renders a
    "not available" note in their place instead).
    """
    run_dir = rundir.run_dir_for(args.study, args.commit)
    comparison_path = run_dir / "comparison.json"
    if not comparison_path.exists():
        print(
            f"{comparison_path}: not found; run the structure-comparator agent first",
            file=sys.stderr,
        )
        return 1
    comparison = schemas.Comparison.model_validate_json(comparison_path.read_text())

    fix_plan = _load_optional_artifact(run_dir / "fix_plan.json", schemas.FixPlan)
    paper_status = _load_optional_artifact(run_dir / "paper_status.json", schemas.PaperStatus)
    materials_manifest = _load_optional_artifact(
        run_dir / "materials_manifest.json", schemas.MaterialsManifest
    )

    report_md, pr_md = report.render(
        comparison, fix_plan, paper_status, materials_manifest, _now_iso()
    )
    (run_dir / "report.md").write_text(report_md)
    (run_dir / "pr.md").write_text(pr_md)

    print(str(run_dir))
    return 0


def _cmd_apply_fixes(args: argparse.Namespace) -> int:
    """Apply a study's `fix_plan.json` via `fixer.apply_fixes` -- see that
    module's docstring for the full worktree/branch/commit/lint-regression
    lifecycle. This subcommand is a thin locate-the-run-dir wrapper; all
    the safety-critical logic lives in fixer.py so it can be unit-tested
    directly against a throwaway git repo (tests/test_fixer.py) without
    going through argparse.
    """
    run_dir = rundir.run_dir_for(args.study, args.commit)
    return fixer.apply_fixes(run_dir, dry_run=args.dry_run, force_branch=args.force_branch)


def _cmd_run(args: argparse.Namespace) -> int:
    """Headless full-pipeline batch runner (task 11, `headless.py`).

    Studies come from either the positional `study` argument or
    `--studies-from FILE` (one study name per line; blank lines and `#`
    comments ignored) — exactly one of the two is expected, `--studies-from`
    taking precedence if both are somehow given, since it's the more
    specific ask. Neither given is a usage error (exit 2): there is nothing
    to run.

    Exit codes: 0 if every study completed without a fatal error, 1 if any
    study's `StudyResult.error` is set (a `StudyFatalError`, e.g. the study
    doesn't exist in the datasets repo) — mirroring `_cmd_lint`'s "1 means
    something needs attention" convention. A study reaching this non-fatal
    outcome still had its run dir printed; only the fatal ones are silent
    apart from the stderr message `headless.run` already printed for them.
    """
    if args.studies_from:
        studies = [
            line.strip()
            for line in Path(args.studies_from).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    elif args.study:
        studies = [args.study]
    else:
        print("run: either <study> or --studies-from FILE is required", file=sys.stderr)
        return 2

    results = headless.run(
        studies, commit=args.commit, force=args.force, max_turns=args.max_turns
    )

    exit_code = 0
    for result in results:
        if result.error is not None:
            exit_code = 1
        else:
            print(str(result.run_dir))
    return exit_code


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

    extract_paper_parser = subparsers.add_parser(
        "extract-paper",
        help="Locate+extract a study's paper.pdf and write a validated paper_status.json.",
    )
    extract_paper_parser.add_argument("study", help="Study folder name under studies/.")
    extract_paper_parser.add_argument(
        "--commit",
        default=None,
        help=(
            "Pin the run dir to a datasets-repo commit (runs/<study>@<shortsha>); "
            "paper.pdf itself is always read from the working tree, never this commit."
        ),
    )
    extract_paper_parser.set_defaults(func=_cmd_extract_paper)

    download_parser = subparsers.add_parser(
        "download",
        help="Download pending sources from a study's materials_manifest.json.",
    )
    download_parser.add_argument("study", help="Study folder name under studies/.")
    download_parser.add_argument(
        "--commit",
        default=None,
        help="Pin to a datasets-repo commit (run dir becomes runs/<study>@<shortsha>).",
    )
    download_parser.set_defaults(func=_cmd_download)

    render_parser = subparsers.add_parser(
        "render",
        help=(
            "Render comparison.json (+ optional fix_plan/paper_status/"
            "materials_manifest) into report.md + pr.md."
        ),
    )
    render_parser.add_argument("study", help="Study folder name under studies/.")
    render_parser.add_argument(
        "--commit",
        default=None,
        help="Pin to a datasets-repo commit (run dir becomes runs/<study>@<shortsha>).",
    )
    render_parser.set_defaults(func=_cmd_render)

    apply_fixes_parser = subparsers.add_parser(
        "apply-fixes",
        help=(
            "Apply fix_plan.json's fixes to the datasets repo via a git "
            "worktree + branch (never the main working tree)."
        ),
    )
    apply_fixes_parser.add_argument("study", help="Study folder name under studies/.")
    apply_fixes_parser.add_argument(
        "--commit",
        default=None,
        help="Pin to a datasets-repo commit (run dir becomes runs/<study>@<shortsha>).",
    )
    apply_fixes_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview each fix as a diff; write nothing, create no branch/worktree.",
    )
    apply_fixes_parser.add_argument(
        "--force-branch",
        action="store_true",
        help="Delete and recreate fix/<study>-structure if it already exists.",
    )
    apply_fixes_parser.set_defaults(func=_cmd_apply_fixes)

    run_parser = subparsers.add_parser(
        "run",
        help="Headless: run the full pipeline for one study (or --studies-from a file).",
    )
    run_parser.add_argument(
        "study",
        nargs="?",
        default=None,
        help="Study folder name under studies/. Omit when using --studies-from.",
    )
    run_parser.add_argument(
        "--commit",
        default=None,
        help="Pin to a datasets-repo commit (run dir becomes runs/<study>@<shortsha>).",
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run every stage even if init-run reports it done.",
    )
    run_parser.add_argument(
        "--studies-from",
        default=None,
        help="Path to a file of study names (one per line; blank/# lines ignored), run sequentially.",
    )
    run_parser.add_argument(
        "--max-turns",
        type=int,
        default=60,
        help="--max-turns passed to every agentic stage's `claude -p` invocation (default: 60).",
    )
    run_parser.set_defaults(func=_cmd_run)

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

    Every subcommand now has a real `func` (task 11's `run` was the last
    placeholder — see `STUB_SUBCOMMANDS`'s docstring). The stub-dispatch
    branch below is kept rather than deleted: it's the harness the next new
    subcommand's task would reuse, exactly as `run` did until this task.
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
