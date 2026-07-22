"""Run-dir lifecycle: the stage registry, `run_meta.json` read/write, and
`stage_status`/`record_stage` resumability logic.

Why this module exists (constraints.md's pipeline-stage table, and Task 3's
note that its `_run_dir_for` placeholder should be absorbed here): every
run's artifacts live under one `runs/<study>[@<shortsha>]/` directory, and
every later task (the `/check-study` skill and the headless `run` command)
needs to answer the same question before doing any work — "for this run
dir, which stages are already done, which are stale because an upstream
artifact changed, and which still need to run?" `STAGE_REGISTRY` below is
the single authoritative statement of pipeline order, artifact filenames,
and inter-stage dependencies; `stage_status`/`record_stage` are the only
things that read/write `run_meta.json`, so no other module should touch it
directly.

Two semantics worth calling out explicitly (both pre-answered
clarifications from the task brief, not judgment calls made here):

- **Artifact-first resumability.** `snapshot`/`lint` don't (yet — that's
  Tasks 10/11's orchestration wiring) call `record_stage` themselves, so a
  stage can have a valid artifact on disk with *no* `run_meta.json` record
  at all. In that case `stage_status` reports "done" on the strength of the
  artifact alone; hash-based staleness checking only kicks in once a
  record exists to compare against. This means a stage can only be
  detected "stale" after at least one `record_stage` call for it.
- **Invalid-artifact handling.** If a stage's own artifact file exists but
  fails schema validation, that is reported as "missing" (not a new status
  value) plus a warning printed to stderr — "missing" because that's the
  status a caller should act on (the stage still needs to (re)run), and the
  warning because a corrupt-but-present file is a more suspicious situation
  than a merely-absent one and is worth flagging.

One more scoping note: hashing an *input* artifact for staleness detection
only ever reads its raw bytes (`_hash_artifact` below) — it does not
require that input to itself validate against its own schema. Schema
validation is only performed on a stage's *own* artifact.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from coggym_check import __version__, config, schemas

StageStatus = Literal["done", "stale", "missing", "skipped", "failed"]


@dataclass(frozen=True)
class StageDef:
    """One pipeline stage's registry entry.

    `inputs` names upstream *stages* (not artifact filenames directly) —
    the artifact to hash for each is looked up via
    `STAGE_REGISTRY[input_stage].artifact`, so a stage's dependency list
    stays readable and never needs to repeat filenames.
    """

    artifact: str
    inputs: tuple[str, ...]


#: The authoritative pipeline order, used later by the `/check-study` skill
#: (Task 10) and the headless runner (Task 11) — these exact stage names and
#: this exact order are load-bearing. Every mapping below is taken verbatim
#: from the task-5 brief's stage-registry line.
STAGE_REGISTRY: dict[str, StageDef] = {
    "snapshot": StageDef(artifact="study_snapshot.json", inputs=()),
    "lint": StageDef(artifact="lint.json", inputs=()),
    "paper": StageDef(artifact="paper_status.json", inputs=("snapshot",)),
    "materials": StageDef(artifact="materials_manifest.json", inputs=("snapshot", "paper")),
    "paper_summary": StageDef(artifact="paper_summary.json", inputs=("snapshot", "paper")),
    "materials_summary": StageDef(artifact="materials_summary.json", inputs=("materials",)),
    "comparison": StageDef(
        artifact="comparison.json",
        inputs=("snapshot", "lint", "paper_summary", "materials_summary"),
    ),
    "fix": StageDef(artifact="fix_plan.json", inputs=("comparison",)),
    "render": StageDef(artifact="report.md", inputs=("comparison", "fix")),
}


def _now_iso() -> str:
    """ISO-8601 UTC timestamp, matching dataset.py's `_now_iso` format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_dir_for(study: str, commit: str | None) -> Path:
    """Return (creating if absent) `runs/<study>` or `runs/<study>@<shortsha>`.

    The one place that owns run-dir naming (constraints.md hard rule #10:
    `shortsha` = the first 9 characters of the commit). `snapshot` and
    `lint`'s CLI subcommands call this instead of deriving the path
    themselves, so naming can never drift between subcommands.
    """
    name = study if commit is None else f"{study}@{commit[:9]}"
    run_dir = config.runs_dir() / name
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _parse_run_dir_name(run_dir: Path) -> tuple[str, str | None]:
    """Recover `(study, commit)` from a run dir's own name.

    Used only as a fallback when `record_stage` needs to create a brand-new
    `run_meta.json` for a run dir that `init_run` never initialized. Note
    this recovers only the *short* sha baked into the directory name (9
    chars), not the full commit `snapshot`/`lint` may have recorded inside
    `study_snapshot.json`/`lint.json` — callers that need the full commit
    preserved should call `init_run` up front instead of relying on this.
    """
    name = run_dir.name
    if "@" in name:
        study, commit = name.split("@", 1)
        return study, commit
    return name, None


def _hash_artifact(path: Path) -> str:
    """sha256 hex digest of `path`'s raw bytes, or the sentinel `"missing"`
    if it doesn't exist.

    `"missing"` is deliberately not a valid hex digest shape, so a reader of
    `run_meta.json` can immediately tell "this input didn't exist yet when
    recorded" apart from any real hash.
    """
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_valid(path: Path) -> bool:
    """Whether `path` validates against the schema its basename implies.

    Filenames with no schema entry (currently just `report.md` — Markdown,
    not a pydantic-validated JSON artifact) are trivially "valid" once
    present: there is nothing to check them against.
    """
    model = schemas.FILENAME_MODEL_MAP.get(path.name)
    if model is None:
        return True
    try:
        model.model_validate_json(path.read_text())
    except (OSError, ValueError, ValidationError):
        return False
    return True


def _load_run_meta(run_dir: Path) -> schemas.RunMeta | None:
    """Read+validate `run_meta.json`, or `None` if it doesn't exist yet."""
    path = run_dir / "run_meta.json"
    if not path.exists():
        return None
    return schemas.RunMeta.model_validate_json(path.read_text())


def _write_run_meta(run_dir: Path, run_meta: schemas.RunMeta) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_meta.json").write_text(run_meta.model_dump_json(indent=2))


def stage_status(run_dir: Path) -> dict[str, StageStatus]:
    """Compute every registered stage's resumability status for `run_dir`.

    Evaluation order per stage (mirrors the task brief's bullet list):
    1. Artifact missing -> "skipped"/"failed" if `run_meta.json` recorded
       that outcome, else "missing".
    2. Artifact present but fails schema validation -> "missing" (+ a
       stderr warning; see module docstring on why "missing" rather than a
       new status value).
    3. Artifact present and valid, stage has no inputs, or no `run_meta`
       record exists yet for it -> "done" (artifact-first semantics: hash
       comparison only happens once a record exists to compare against).
    4. Artifact present and valid, record exists, stage has inputs ->
       "done" if every input's current hash matches the hash recorded at
       `record_stage` time, else "stale".
    """
    run_meta = _load_run_meta(run_dir)
    statuses: dict[str, StageStatus] = {}

    for stage, stage_def in STAGE_REGISTRY.items():
        artifact_path = run_dir / stage_def.artifact
        record = run_meta.stages.get(stage) if run_meta is not None else None

        if not artifact_path.exists():
            if record is not None and record.status == "skipped":
                statuses[stage] = "skipped"
            elif record is not None and record.status == "failed":
                statuses[stage] = "failed"
            else:
                statuses[stage] = "missing"
            continue

        if not _artifact_valid(artifact_path):
            print(
                f"warning: {stage_def.artifact} exists but failed schema "
                f"validation; treating stage '{stage}' as missing",
                file=sys.stderr,
            )
            statuses[stage] = "missing"
            continue

        if not stage_def.inputs or record is None:
            statuses[stage] = "done"
            continue

        current_hashes = {
            input_stage: _hash_artifact(run_dir / STAGE_REGISTRY[input_stage].artifact)
            for input_stage in stage_def.inputs
        }
        statuses[stage] = "done" if current_hashes == record.input_hashes else "stale"

    return statuses


def record_stage(
    run_dir: Path,
    stage: str,
    status: Literal["done", "skipped", "failed"],
    *,
    skipped_reason: str | None = None,
    claude: schemas.ClaudeInvocationMeta | None = None,
) -> schemas.RunMeta:
    """Record `stage`'s outcome into `run_meta.json`, computing its input
    hashes (the current bytes of every upstream stage's artifact) at this
    moment.

    Creates `run_meta.json` if it doesn't exist yet (falling back to
    `_parse_run_dir_name` for `study`/`commit` — see its docstring for the
    shortsha caveat this implies). Prefer calling `init_run` first so the
    full commit is preserved; this fallback exists so `record_stage` never
    hard-requires that ordering.
    """
    if stage not in STAGE_REGISTRY:
        known = ", ".join(STAGE_REGISTRY)
        raise ValueError(f"unknown stage '{stage}'; known stages: {known}")
    stage_def = STAGE_REGISTRY[stage]

    run_meta = _load_run_meta(run_dir)
    if run_meta is None:
        study, commit = _parse_run_dir_name(run_dir)
        run_meta = schemas.RunMeta(
            study=study,
            commit=commit,
            created_at=_now_iso(),
            tool_version=__version__,
            stages={},
        )

    input_hashes = {
        input_stage: _hash_artifact(run_dir / STAGE_REGISTRY[input_stage].artifact)
        for input_stage in stage_def.inputs
    }

    run_meta.stages[stage] = schemas.StageMeta(
        status=status,
        artifact=stage_def.artifact if status == "done" else None,
        input_hashes=input_hashes,
        skipped_reason=skipped_reason,
        claude=claude,
    )
    _write_run_meta(run_dir, run_meta)
    return run_meta


def init_run(study: str, commit: str | None = None) -> Path:
    """Create (or reuse) `runs/<study>[@<shortsha>]/` and its `run_meta.json`.

    Idempotent: an existing `run_meta.json` is never overwritten (per the
    task brief's pre-answered clarification), so calling this against an
    in-progress run dir is always safe and never clobbers recorded stages.
    """
    run_dir = run_dir_for(study, commit)
    if not (run_dir / "run_meta.json").exists():
        run_meta = schemas.RunMeta(
            study=study,
            commit=commit,
            created_at=_now_iso(),
            tool_version=__version__,
            stages={},
        )
        _write_run_meta(run_dir, run_meta)
    return run_dir
