"""Headless batch runner: `python -m coggym_check run <study>` executes the
full check-study pipeline — the same stage registry and skip rules as
`.claude/skills/check-study/SKILL.md` — without an interactive Claude
session driving it.

Why this module exists instead of reusing the skill directly: the skill is
written for an interactive Claude session (it launches subagents via the
`Task` tool, which only exists inside that session). A headless batch run
over many studies needs the equivalent orchestration expressed as plain
Python that shells out to the `claude` CLI itself for each agentic stage —
one `claude -p ... --output-format json` subprocess per agentic stage,
parsed for its result artifact + cost/duration/session metadata, which this
module (unlike the deterministic-stage CLI subcommands in `cli.py`) records
into `run_meta.json` via `rundir.record_stage` for every stage it runs, so a
batch run gets full resumability + cost bookkeeping without a human present
to do that bookkeeping by hand.

Agent-file frontmatter parsing (`parse_agent_file`) is a **minimal hand-rolled
parser, not pyyaml** — pyyaml is not installed in this project's venv
(checked: `import yaml` fails), and constraints.md hard rule #1 forbids
adding a dependency without noting it in `requirements.txt` with a pinned
version. Every field this pipeline's agent frontmatter actually uses is a
flat `key: value` line (never nested structures, lists-of-mappings, or
multi-line scalars), so a full YAML parser would be solving a much bigger
problem than the one six checked-in files actually pose. See
`_parse_frontmatter_lines`'s docstring for the one real wrinkle it does
handle (a quoted value, `paper-finder.md`'s `description`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from coggym_check import config, dataset, lint, paper, rundir, schemas

#: Default `--max-turns` for every agentic-stage `claude -p` invocation
#: (task-11 brief). Overridable per `run` invocation via `--max-turns`.
DEFAULT_MAX_TURNS = 60

#: Recorded for a stage whose `claude -p` subprocess never spawned at all
#: (`FileNotFoundError`/`OSError`/`subprocess.SubprocessError`) — there is no
#: cost/duration/session to report since no invocation actually happened, so
#: this mirrors `_parse_claude_output`'s own malformed-stdout fallback value
#: rather than inventing a new "no data" convention.
_ZEROED_CLAUDE_META = schemas.ClaudeInvocationMeta(cost_usd=0.0, duration_s=0.0, session_id="")


class StudyFatalError(RuntimeError):
    """Raised when one study's pipeline cannot proceed at all.

    Currently only `run_snapshot_stage` raises this (mirroring
    `_cmd_snapshot`'s "exit 1 is fatal" contract for `StudyNotFoundError`:
    nothing downstream can run without a snapshot). `run`'s multi-study loop
    catches exactly this exception type so one study missing from the
    datasets repo doesn't abort a `--studies-from` batch.
    """


# ---------------------------------------------------------------------------
# Agent-file parsing (no pyyaml — see module docstring)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """One `.claude/agents/*.md` file's frontmatter + body.

    `tools`/`model` feed directly into `build_claude_command`'s
    `--allowedTools`/`--model` flags; `body` becomes `--append-system-prompt`
    verbatim (it is the agent's entire Markdown "Mission"/"Procedure"/
    "Output contract"/"Failure modes" prose, unchanged).
    """

    name: str
    description: str
    tools: list[str]
    model: str
    body: str


def _parse_frontmatter_lines(lines: list[str]) -> dict[str, str]:
    """Parse `key: value` lines from between a Markdown file's `---`
    delimiters into a flat dict of raw (string) values.

    The one wrinkle real frontmatter here exercises: a value wrapped in
    matching quotes (`paper-finder.md`'s `description`, quoted for some
    YAML-safety reason even though its content turns out not to contain an
    unescaped colon) must come back with the quotes stripped. `partition(":")`
    only ever splits on the *first* colon on the line, so everything after
    it — quotes, any colons the quoted text itself might contain, anything —
    is treated as the raw value; only *then* do we check whether that raw
    value is wrapped in a matching pair of quote characters and strip them.
    This means a value containing a colon is handled correctly by
    construction, quoted or not, without this parser needing to understand
    YAML's own quoting/escaping rules in general.
    """
    fields: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key] = value
    return fields


def parse_agent_file(path: Path) -> AgentSpec:
    """Parse one `.claude/agents/<name>.md` file into an `AgentSpec`.

    Raises `ValueError` if the file doesn't open with a `---` frontmatter
    delimiter or the delimiter is never closed — both indicate a malformed
    agent file, not a runtime condition this module should quietly paper
    over into an empty spec.
    """
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path}: expected YAML frontmatter starting with '---'")
    try:
        end_idx = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError(f"{path}: unterminated frontmatter (no closing '---')") from None

    fields = _parse_frontmatter_lines(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :]).lstrip("\n")

    tools = [t.strip() for t in fields.get("tools", "").split(",") if t.strip()]

    return AgentSpec(
        name=fields.get("name", ""),
        description=fields.get("description", ""),
        tools=tools,
        model=fields.get("model", ""),
        body=body,
    )


def load_agent(name: str) -> AgentSpec:
    """Parse `.claude/agents/<name>.md` under this repo's root."""
    return parse_agent_file(config.repo_root() / ".claude" / "agents" / f"{name}.md")


# ---------------------------------------------------------------------------
# `claude` CLI command + stage prompt construction
# ---------------------------------------------------------------------------


def build_claude_command(agent: AgentSpec, prompt: str, max_turns: int) -> list[str]:
    """Build the exact argv for one agentic stage's `claude -p` invocation.

    `--allowedTools` joins `agent.tools` with `,` (matching the comma-
    separated shape the tools already have in frontmatter, e.g.
    `"Read, Write, Bash"`) — this is an assumption about the real `claude`
    CLI's accepted separator (not verified against its own docs here), kept
    in exactly one place so it's a one-line change if that assumption turns
    out to be wrong.
    """
    return [
        "claude",
        "-p",
        prompt,
        "--append-system-prompt",
        agent.body,
        "--allowedTools",
        ",".join(agent.tools),
        "--model",
        agent.model,
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
    ]


def build_stage_prompt(
    study: str,
    run_dir: Path,
    input_paths: list[Path],
    output_path: Path,
    *,
    also_produces: Path | None = None,
    commit: str | None = None,
) -> str:
    """Build one stage's task prompt from a single shared template: study,
    run dir, input artifact paths, output artifact path, and a closing
    instruction to validate before finishing.

    `also_produces` covers the one stage pairing that isn't 1-in/1-out:
    `fix-drafter` writes both `fix_plan.json` (`output_path`) and
    `report.md`/`pr.md` (via its own `render` call) in one invocation.

    `commit`, when set (a `run --commit <sha>` pin), must be surfaced to the
    agent explicitly: the agent's own `python -m coggym_check` calls
    (`download`, `apply-fixes`, `render`, ...) need `--commit <sha>` too, or
    they resolve to `runs/<study>/` instead of `runs/<study>@<shortsha>` and
    (for `apply-fixes`) branch off HEAD instead of the pinned commit.
    """
    inputs_str = ", ".join(str(p) for p in input_paths) if input_paths else "none"
    parts = [
        f"Study: {study}.",
        f"Run dir: {run_dir}.",
        f"Input artifacts: {inputs_str}.",
        f"Write {output_path}.",
    ]
    if also_produces is not None:
        parts.append(f"Also produce {also_produces} (and pr.md) per your output contract.")
    if commit is not None:
        parts.append(
            f"Commit: {commit} — pass --commit {commit} to every python -m coggym_check "
            "command you run."
        )
    parts.append("Finish by running validate-artifact on every artifact you wrote.")
    return " ".join(parts)


def _parse_claude_output(stdout: str) -> schemas.ClaudeInvocationMeta:
    """Parse `claude -p --output-format json`'s stdout into cost/duration/
    session bookkeeping for `record_stage`.

    Assumed shape (observed from real `claude -p --output-format json`
    invocations, not from a published/versioned schema): a single JSON
    object with (at least) `result` (the final text response, unused here),
    `total_cost_usd` (float), `duration_ms` (int), and `session_id` (str).
    Since that shape isn't a contractually stable API, every field is read
    with `.get` and defaulted rather than required — malformed or
    unexpectedly-shaped stdout (including non-JSON stdout, e.g. a crashed
    `claude` process) degrades to a zeroed-out `ClaudeInvocationMeta` rather
    than raising, so one stage's bad output can never take down the whole
    batch run.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    cost = data.get("total_cost_usd")
    duration_ms = data.get("duration_ms")
    session_id = data.get("session_id")

    return schemas.ClaudeInvocationMeta(
        cost_usd=float(cost) if isinstance(cost, (int, float)) else 0.0,
        duration_s=float(duration_ms) / 1000.0 if isinstance(duration_ms, (int, float)) else 0.0,
        session_id=str(session_id) if session_id is not None else "",
    )


def _artifact_is_valid(path: Path) -> bool:
    """Whether `path` exists and (if it has a schema) validates.

    Mirrors `rundir._artifact_valid`'s semantics but is kept as its own
    private copy here rather than importing that underscore-prefixed name:
    `rundir`'s is deliberately module-private (used only by `stage_status`),
    and this module has its own reason to need the check — validating a
    just-produced agentic-stage artifact before deciding `done` vs.
    `failed` — that doesn't otherwise need anything else from `rundir`'s
    internals.
    """
    if not path.exists():
        return False
    model = schemas.FILENAME_MODEL_MAP.get(path.name)
    if model is None:
        return True
    try:
        model.model_validate_json(path.read_text())
    except (OSError, ValueError, ValidationError):
        return False
    return True


def _input_artifact_paths(run_dir: Path, stage: str) -> list[Path]:
    """Every input path a stage's prompt should list: its declared upstream
    artifacts (`STAGE_REGISTRY[stage].inputs`), plus its own artifact if one
    already exists on disk (the one case this covers: the `paper` stage's
    deterministic half always writes `paper_status.json` before the
    `paper-finder` agent fallback runs, and that existing file — reporting
    `status: not_found` — is itself context the agent needs to read)."""
    stage_def = rundir.STAGE_REGISTRY[stage]
    paths = [run_dir / rundir.STAGE_REGISTRY[s].artifact for s in stage_def.inputs]
    own_path = run_dir / stage_def.artifact
    if own_path.exists() and own_path not in paths:
        paths.append(own_path)
    return paths


# ---------------------------------------------------------------------------
# Generic agentic-stage runner
# ---------------------------------------------------------------------------


def run_agentic_stage(
    run_dir: Path,
    study: str,
    stage: str,
    agent_name: str,
    max_turns: int,
    *,
    commit: str | None = None,
) -> None:
    """Run one agentic stage: build its prompt, exec `claude -p ...`, parse
    the result, and record `done`/`failed` (+ Claude cost metadata) via
    `record_stage`.

    Never raises on a bad or missing artifact — a stage that fails artifact
    validation is recorded `failed` and control returns to the caller, which
    (per the task brief) must continue on to the next runnable stage rather
    than aborting the whole study.

    `commit`, when this is a `run --commit <sha>` invocation, is threaded
    into the agent's own prompt (`build_stage_prompt`'s `commit` kwarg) so
    the agent's own `python -m coggym_check` calls stay pinned to the same
    commit rather than silently falling back to the unpinned run dir.
    """
    agent = load_agent(agent_name)
    stage_def = rundir.STAGE_REGISTRY[stage]
    output_path = run_dir / stage_def.artifact
    prompt = build_stage_prompt(
        study, run_dir, _input_artifact_paths(run_dir, stage), output_path, commit=commit
    )
    cmd = build_claude_command(agent, prompt, max_turns)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        # The `claude` binary itself is missing/unspawnable (FileNotFoundError
        # is the common case) — this is an environment problem, not a
        # per-stage artifact-validation failure, but it must be contained the
        # same way: recorded `failed` on this one stage so the rest of the
        # batch (remaining stages, remaining studies) keeps going rather than
        # an unhandled OSError/SubprocessError taking down the whole run.
        rundir.record_stage(
            run_dir,
            stage,
            "failed",
            claude=_ZEROED_CLAUDE_META,
            skipped_reason=f"claude subprocess failed to spawn: {exc}",
        )
        return
    claude_meta = _parse_claude_output(proc.stdout)

    status = "done" if _artifact_is_valid(output_path) else "failed"
    rundir.record_stage(run_dir, stage, status, claude=claude_meta)


def run_fix_and_render_stage(
    run_dir: Path, study: str, max_turns: int, *, commit: str | None = None
) -> None:
    """Run the `fix` + `render` stages as the single `fix-drafter` unit the
    skill treats them as: one `claude -p` invocation is expected to write
    both `fix_plan.json` and `report.md` (running `apply-fixes`/`render`
    itself via its own `Bash` tool), so both registry stages are recorded
    from this one invocation's outcome.

    `commit` (a `run --commit <sha>` pin) is threaded into the prompt so the
    agent's own `apply-fixes`/`render` calls stay pinned to the same commit
    (`apply-fixes` in particular must branch off that commit, not HEAD).
    """
    agent = load_agent("fix-drafter")
    fix_output = run_dir / rundir.STAGE_REGISTRY["fix"].artifact
    render_output = run_dir / rundir.STAGE_REGISTRY["render"].artifact
    prompt = build_stage_prompt(
        study,
        run_dir,
        _input_artifact_paths(run_dir, "fix"),
        fix_output,
        also_produces=render_output,
        commit=commit,
    )
    cmd = build_claude_command(agent, prompt, max_turns)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        # Same containment as run_agentic_stage's spawn-failure handling —
        # this unit records *two* registry stages (fix, render) from one
        # invocation, so both get the same failed/zeroed/explanatory record.
        note = f"claude subprocess failed to spawn: {exc}"
        rundir.record_stage(run_dir, "fix", "failed", claude=_ZEROED_CLAUDE_META, skipped_reason=note)
        rundir.record_stage(run_dir, "render", "failed", claude=_ZEROED_CLAUDE_META, skipped_reason=note)
        return
    claude_meta = _parse_claude_output(proc.stdout)

    fix_status = "done" if _artifact_is_valid(fix_output) else "failed"
    # report.md has no pydantic schema (rundir._artifact_valid's own
    # docstring notes this) — its mere presence is the check.
    render_status = "done" if render_output.exists() else "failed"

    rundir.record_stage(run_dir, "fix", fix_status, claude=claude_meta)
    rundir.record_stage(run_dir, "render", render_status, claude=claude_meta)


# ---------------------------------------------------------------------------
# Deterministic stages (called as package functions, not via subprocess —
# constraints.md's guidance: reuse the module functions directly rather than
# subprocess-to-own-CLI).
# ---------------------------------------------------------------------------


def run_snapshot_stage(run_dir: Path, study: str, commit: str | None) -> None:
    """Deterministic `snapshot` stage. Raises `StudyFatalError` on
    `StudyNotFoundError` — the one fatal-per-study condition in this
    pipeline (nothing downstream can run without a snapshot)."""
    try:
        snapshot = dataset.load_study(study, commit=commit)
    except dataset.StudyNotFoundError as exc:
        raise StudyFatalError(str(exc)) from exc

    payload = snapshot.model_dump_json(indent=2)
    schemas.StudySnapshot.model_validate_json(payload)
    (run_dir / "study_snapshot.json").write_text(payload)
    rundir.record_stage(run_dir, "snapshot", "done")


def run_lint_stage(run_dir: Path, study: str, commit: str | None) -> None:
    """Deterministic `lint` stage. `error`-level findings are data, not an
    exception, so this always records `done` once `lint.json` is written —
    per the known behavioral fact that lint errors are non-fatal."""
    try:
        report = lint.lint_study(study, commit=commit)
    except dataset.StudyNotFoundError as exc:
        # Same underlying condition as the snapshot stage's fatal case
        # (the study doesn't exist at all); lint should never actually
        # reach this once snapshot has already succeeded, but guard
        # symmetrically rather than letting a raw exception escape.
        raise StudyFatalError(str(exc)) from exc

    payload = report.model_dump_json(indent=2)
    schemas.LintReport.model_validate_json(payload)
    (run_dir / "lint.json").write_text(payload)
    rundir.record_stage(run_dir, "lint", "done")


def run_paper_stage(run_dir: Path, study: str, commit: str | None, max_turns: int) -> None:
    """Deterministic `extract-paper` first; if it comes back `not_found`,
    fall back to the `paper-finder` agent (mirrors the skill's step 3c).
    Any other outcome (`found_local`) needs no agent."""
    status = paper.extract_paper(study)
    payload = status.model_dump_json(indent=2)
    schemas.PaperStatus.model_validate_json(payload)
    (run_dir / "paper_status.json").write_text(payload)

    if status.status != "not_found":
        rundir.record_stage(run_dir, "paper", "done")
        return

    run_agentic_stage(run_dir, study, "paper", "paper-finder", max_turns, commit=commit)


def run_paper_summary_stage(
    run_dir: Path, study: str, max_turns: int, *, commit: str | None = None
) -> None:
    """`paper_summary` stage, skippable per the skill's rule: skip iff
    `paper_status.json`'s `status` is `not_found` or `paywalled` (no paper
    text exists for `paper-analyst` to read)."""
    paper_status_path = run_dir / "paper_status.json"
    if paper_status_path.exists():
        try:
            paper_status = schemas.PaperStatus.model_validate_json(paper_status_path.read_text())
        except ValidationError:
            paper_status = None
        if paper_status is not None and paper_status.status in ("not_found", "paywalled"):
            rundir.record_stage(
                run_dir,
                "paper_summary",
                "skipped",
                skipped_reason=f"paper_status is {paper_status.status}",
            )
            return

    run_agentic_stage(run_dir, study, "paper_summary", "paper-analyst", max_turns, commit=commit)


def run_materials_summary_stage(
    run_dir: Path, study: str, max_turns: int, *, commit: str | None = None
) -> None:
    """`materials_summary` stage, skippable per the skill's rule: skip iff
    `materials_manifest.json`'s `status` is `none_found` (nothing downloaded
    for `materials-analyst` to mine)."""
    manifest_path = run_dir / "materials_manifest.json"
    if manifest_path.exists():
        try:
            manifest = schemas.MaterialsManifest.model_validate_json(manifest_path.read_text())
        except ValidationError:
            manifest = None
        if manifest is not None and manifest.status == "none_found":
            rundir.record_stage(
                run_dir,
                "materials_summary",
                "skipped",
                skipped_reason="materials_manifest status is none_found",
            )
            return

    run_agentic_stage(
        run_dir, study, "materials_summary", "materials-analyst", max_turns, commit=commit
    )


# ---------------------------------------------------------------------------
# Per-study orchestration
# ---------------------------------------------------------------------------

#: Dispatch order, one entry per registry stage except `render` (handled
#: together with `fix` by `run_fix_and_render_stage` — see that function's
#: docstring). Order matters: it's `rundir.STAGE_REGISTRY`'s own pipeline
#: order with `render` filtered out, so upstream artifacts are always ready
#: before a downstream stage's prompt lists them as inputs.
STAGE_DISPATCH_ORDER: list[str] = [s for s in rundir.STAGE_REGISTRY if s != "render"]


def _dispatch_stage(
    run_dir: Path, study: str, commit: str | None, stage: str, max_turns: int
) -> None:
    if stage == "snapshot":
        run_snapshot_stage(run_dir, study, commit)
    elif stage == "lint":
        run_lint_stage(run_dir, study, commit)
    elif stage == "paper":
        run_paper_stage(run_dir, study, commit, max_turns)
    elif stage == "materials":
        run_agentic_stage(run_dir, study, "materials", "materials-scout", max_turns, commit=commit)
    elif stage == "paper_summary":
        run_paper_summary_stage(run_dir, study, max_turns, commit=commit)
    elif stage == "materials_summary":
        run_materials_summary_stage(run_dir, study, max_turns, commit=commit)
    elif stage == "comparison":
        # Never skipped, even when both paper_summary/materials_summary
        # were — a degraded comparison is still meaningful (skill step 3g).
        run_agentic_stage(
            run_dir, study, "comparison", "structure-comparator", max_turns, commit=commit
        )
    elif stage == "fix":
        run_fix_and_render_stage(run_dir, study, max_turns, commit=commit)
    else:  # pragma: no cover - STAGE_DISPATCH_ORDER is derived from the
        # registry itself, so every real stage name is handled above.
        raise ValueError(f"no dispatch handler for stage '{stage}'")


def run_study(
    study: str,
    *,
    commit: str | None = None,
    force: bool = False,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> Path:
    """Run every pending stage for one study, in registry order, and return
    its run dir.

    "Pending" mirrors the skill's step-2 decision rule: a stage runs if its
    current status is `missing`/`stale`, or unconditionally if `force` is
    set; a `done` or `skipped` stage is left alone otherwise. Status is
    re-read fresh before every stage (not computed once up front) since a
    stage just completed can flip a downstream stage from `missing` to
    `stale` (its input hash changed) or vice versa.

    A single stage failing (agentic artifact-validation failure, or a
    skipped-then-still-flagged input) never stops this loop — every stage in
    `STAGE_DISPATCH_ORDER` is still attempted. Only `StudyFatalError` (from
    `run_snapshot_stage`) propagates out of this function, for `run` to
    catch per-study.
    """
    run_dir = rundir.init_run(study, commit)
    for stage in STAGE_DISPATCH_ORDER:
        statuses = rundir.stage_status(run_dir)
        status = statuses[stage]
        if stage == "fix":
            # `fix` and `render` are one dispatch unit (run_fix_and_render_stage)
            # even though `render` isn't its own entry in STAGE_DISPATCH_ORDER,
            # so gating re-dispatch on `statuses["fix"]` alone misses the case
            # where fix-drafter wrote a valid fix_plan.json but crashed before
            # render (render left `failed`/`missing`) — a plain re-run would
            # then see fix "done" and never re-attempt render. Gate on either
            # stage being incomplete instead; re-dispatching re-runs the
            # fix-drafter agent, which is expected to find fix_plan.json
            # already done and proceed straight to render (its own output
            # contract handles that idempotency, not this loop).
            if (
                not force
                and statuses["fix"] in ("done", "skipped")
                and statuses["render"] in ("done", "skipped")
            ):
                continue
        elif not force and status in ("done", "skipped"):
            continue
        _dispatch_stage(run_dir, study, commit, stage, max_turns)
    return run_dir


# ---------------------------------------------------------------------------
# Multi-study orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StudyResult:
    """One study's outcome from a `run` batch: either its run dir, or the
    fatal error message that stopped it (never both)."""

    study: str
    run_dir: Path | None
    error: str | None


def run(
    studies: list[str],
    *,
    commit: str | None = None,
    force: bool = False,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> list[StudyResult]:
    """Run `run_study` sequentially over every study in `studies`.

    A `StudyFatalError` for one study is caught here (printed to stderr) and
    recorded as that study's `StudyResult.error` — the loop always continues
    on to the next study rather than aborting the whole batch, per the known
    behavioral fact that a fatal snapshot failure is fatal only *for that
    study*.
    """
    results: list[StudyResult] = []
    for study in studies:
        try:
            run_dir = run_study(study, commit=commit, force=force, max_turns=max_turns)
        except StudyFatalError as exc:
            print(f"{study}: fatal error, skipping: {exc}", file=sys.stderr)
            results.append(StudyResult(study=study, run_dir=None, error=str(exc)))
            continue
        results.append(StudyResult(study=study, run_dir=run_dir, error=None))
    return results
