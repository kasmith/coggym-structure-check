"""`apply-fixes`: the only code in this project allowed to write into the
`cog-gym-datasets` clone, and only ever through a `git worktree` — never its
main working tree, index, or HEAD (constraints.md hard rule #3: that clone
is a detached-HEAD checkout with untracked scaffolding files that must never
be disturbed).

Lifecycle, in order:

1. Read `fix_plan.json`. Empty `fixes` -> print a note, write nothing,
   exit 0 (nothing to do; this is not an error).
2. Resolve the base commit: the run dir's own pinned `@<shortsha>` if its
   name has one, else the datasets repo's current `HEAD` (pre-answered
   clarification — see the task-8 brief).
3. `--dry-run`: for every proposed fix, read its target file's *current*
   content at `base` via `git show` (never touching the working tree) and
   print a unified diff of the in-memory edit. Writes nothing, creates no
   branch/worktree.
4. Otherwise: refuse (exit 1) if `fix/<study>-structure` already exists,
   unless `--force-branch` (delete + recreate). `git worktree add` that
   branch off `base` at `<run_dir>/.worktree` — this is the one git
   operation in this whole module that mutates the datasets repo, and it
   only ever registers a new worktree/branch, never touching the existing
   working tree/index/HEAD.
5. Apply every fix, one `git commit` per (experiment, category) group (in
   sorted order, for determinism), *inside the worktree only*.
6. Re-lint the worktree (Task 4's `lint.lint_study`, pointed at the
   worktree via its `repo_root` parameter — see lint.py's task-8 addition).
   If that produces more error-level findings than the run dir's existing
   (pre-fix) `lint.json`, the whole thing is treated as failed: the branch
   is deleted (so it never persists half-broken) and this returns exit 1.
7. Always (success, failure, or an exception mid-apply): `git worktree
   remove --force` the worktree and `git worktree prune` — via a
   `try/finally`, so a crash never leaves a stray worktree registered
   against the real datasets repo. The branch itself persists *only* if
   every commit succeeded and lint didn't regress; every other path deletes
   it, so `apply-fixes` never leaves a half-applied branch lying around.

`git push` never appears anywhere in this module (grep it) — pushing to the
datasets repo's remote is explicitly out of scope forever (constraints.md
hard rule #3).

Fix-op category mapping (used to group commits, and mirrored in report.py
for the PR body's "Auto-fixed" summary — kept as a private duplicate in each
module rather than a shared import, since it's a small fixed table): the
four `FixOp` types line up 1:1 with constraints.md's four exhaustive
auto-fixable categories (decision 4), so no `comparison.json` lookup is
needed to know a fix's category.

JSONL surgical edit (task-8 pre-answered clarification, verified against
`../cog-gym-datasets/studies/Gerstenberg2015How/exp1/instruction.jsonl`):
only the target line is re-serialized, via
`json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))`; every other
line is carried over as the exact original string (including whatever line
terminator it already had), so untouched lines are byte-for-byte identical.
`config.json` edits go through `json.load`/`json.dumps(indent=2)` — a plain
`dict` preserves key insertion order, so round-tripping through it preserves
key order automatically.
"""

from __future__ import annotations

import difflib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from coggym_check import config, lint, rundir, schemas
from coggym_check.schemas import FixEntry, FixOp

#: `FixOp.op` -> the one constraints.md fix-conservatism category it always
#: belongs to. See module docstring.
_OP_CATEGORY: dict[str, str] = {
    "set_instruction_text": "instruction_text",
    "set_block_randomization": "randomization",
    "set_slider_labels": "response_scale",
    "set_config_field": "citation",
}

#: Category -> short human phrase for a commit subject line.
_CATEGORY_SUMMARY: dict[str, str] = {
    "instruction_text": "update instruction text to match verbatim materials",
    "randomization": "correct block randomization flags",
    "response_scale": "correct slider labels/range",
    "citation": "correct citation metadata",
}

#: `json.dumps` separators matching the real dataset's `instruction.jsonl`
#: style (verified against Gerstenberg2015How/exp1 — see module docstring).
_JSONL_SEPARATORS = (", ", ": ")


class ApplyFixesError(RuntimeError):
    """Raised for a fix that cannot be located/applied (e.g. an unknown
    `record_id`/`trial_id`) — callers convert this into an aborted run
    (worktree removed, branch deleted) rather than a stray partial commit.
    """


# ---------------------------------------------------------------------------
# git plumbing — every call here is scoped to `repo` via `git -C`, and the
# only one that mutates `repo` itself (as opposed to a worktree hanging off
# it) is `_add_worktree`/`_delete_branch`/`_remove_worktree`'s branch and
# worktree-registration bookkeeping. None of this ever runs `git checkout`,
# `git reset`, or touches `repo`'s index/HEAD.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def _current_head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return result.returncode == 0


def _delete_branch(repo: Path, branch: str) -> None:
    _git(repo, "branch", "-D", branch, check=False)


def _add_worktree(repo: Path, path: Path, branch: str, base: str) -> None:
    if path.exists():
        # Leftover from a previous crashed run -- clear it before retrying,
        # rather than letting `worktree add` fail on a non-empty directory.
        _git(repo, "worktree", "remove", "--force", str(path), check=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", str(path), "-b", branch, base)


def _remove_worktree(repo: Path, path: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(path), check=False)
    _git(repo, "worktree", "prune", check=False)


def _show(repo: Path, commitish: str, relpath: str) -> str:
    result = _git(repo, "show", f"{commitish}:{relpath}")
    return result.stdout


# ---------------------------------------------------------------------------
# Fix-op -> file transform. Pure text-in/text-out so the exact same code
# path both computes the --dry-run diff preview and the real worktree edit
# (see module docstring).
# ---------------------------------------------------------------------------


def _relpath_for_fix(study: str, fix: FixOp) -> str:
    exp = fix.experiment
    if fix.op in ("set_config_field", "set_block_randomization"):
        return f"studies/{study}/{exp}/config.json"
    if fix.op == "set_instruction_text":
        return f"studies/{study}/{exp}/instruction.jsonl"
    if fix.op == "set_slider_labels":
        return f"studies/{study}/{exp}/trial.jsonl"
    raise ValueError(f"unknown fix op {fix.op!r}")  # pragma: no cover — exhaustive union


def _set_by_key_path(container: Any, key_path: str, new_value: Any) -> None:
    """Walk `key_path` (dot-separated; a segment indexes a list with `int()`
    whenever the container at that point *is* a list — see module docstring
    on `set_config_field`'s convention) and overwrite the final segment."""
    parts = key_path.split(".")
    obj = container
    for part in parts[:-1]:
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    last = parts[-1]
    if isinstance(obj, list):
        obj[int(last)] = new_value
    else:
        obj[last] = new_value


def _mutate_config_text(text: str, mutate: Callable[[dict], None]) -> str:
    data = json.loads(text)
    mutate(data)
    return json.dumps(data, indent=2, ensure_ascii=False)


def _mutate_jsonl_text(text: str, match_id: str, mutate: Callable[[dict], None]) -> str:
    """Rewrite only the JSONL line whose `id` equals `match_id`; every other
    line (including blank lines) is carried over as its exact original
    string, terminator and all — see module docstring."""
    lines = text.splitlines(keepends=True)
    out_lines: list[str] = []
    found = False
    for line in lines:
        stripped = line.rstrip("\r\n")
        terminator = line[len(stripped) :]
        if not stripped.strip():
            out_lines.append(line)
            continue
        obj = json.loads(stripped)
        if obj.get("id") == match_id:
            mutate(obj)
            out_lines.append(
                json.dumps(obj, ensure_ascii=False, separators=_JSONL_SEPARATORS) + terminator
            )
            found = True
        else:
            out_lines.append(line)
    if not found:
        raise ApplyFixesError(f"record id {match_id!r} not found while applying a fix")
    return "".join(out_lines)


def _compute_new_content(old_text: str, fix: FixOp) -> str:
    """Apply one `FixOp` to `old_text`, returning the new file content."""
    if fix.op == "set_config_field":
        return _mutate_config_text(
            old_text, lambda data: _set_by_key_path(data, fix.key_path, fix.new_value)
        )
    if fix.op == "set_block_randomization":

        def _mutate_br(data: dict) -> None:
            data["experimentFlow"][fix.condition_index]["block_randomization"][
                fix.block_index
            ] = fix.new_value

        return _mutate_config_text(old_text, _mutate_br)
    if fix.op == "set_instruction_text":

        def _mutate_text(obj: dict) -> None:
            obj["text"] = fix.new_value

        return _mutate_jsonl_text(old_text, fix.record_id, _mutate_text)
    if fix.op == "set_slider_labels":

        def _mutate_slider(obj: dict) -> None:
            for query in obj.get("queries", []):
                if query.get("tag") == fix.query_tag:
                    slider_config = query.setdefault("slider_config", {})
                    if fix.new_labels is not None:
                        slider_config["labels"] = fix.new_labels
                    if fix.new_min is not None:
                        slider_config["min"] = fix.new_min
                    if fix.new_max is not None:
                        slider_config["max"] = fix.new_max

        return _mutate_jsonl_text(old_text, fix.trial_id, _mutate_slider)
    raise ValueError(f"unknown fix op {fix.op!r}")  # pragma: no cover — exhaustive union


# ---------------------------------------------------------------------------
# Grouping + commit messages
# ---------------------------------------------------------------------------


def _group_key(fix: FixOp) -> tuple[str, str]:
    return (fix.experiment, _OP_CATEGORY[fix.op])


def _grouped_sorted(fixes: list[FixEntry]) -> list[tuple[tuple[str, str], list[FixEntry]]]:
    """Group `fixes` by `(experiment, category)`, each group's entries sorted
    by `discrepancy_id`, groups themselves in sorted key order — the
    determinism the task brief calls for."""
    groups: dict[tuple[str, str], list[FixEntry]] = {}
    for entry in fixes:
        groups.setdefault(_group_key(entry.fix), []).append(entry)
    return [
        (key, sorted(groups[key], key=lambda e: e.discrepancy_id)) for key in sorted(groups)
    ]


def _commit_message(
    study: str, experiment: str, category: str, entries: list[FixEntry]
) -> str:
    subject = f"fix({study}/{experiment}): {_CATEGORY_SUMMARY[category]}"
    body_lines = ["", "Discrepancies fixed:"]
    for entry in entries:
        body_lines.append(f"- {entry.discrepancy_id}: {entry.commit_message}")
    return subject + "\n" + "\n".join(body_lines) + "\n"


def _apply_fix_to_worktree(worktree_path: Path, study: str, fix: FixOp) -> str:
    """Apply one fix directly to the worktree's file, returning the relpath
    (relative to `worktree_path`) it touched — for `git add`."""
    relpath = _relpath_for_fix(study, fix)
    path = worktree_path / relpath
    old_text = path.read_text(encoding="utf-8")
    new_text = _compute_new_content(old_text, fix)
    path.write_text(new_text, encoding="utf-8")
    return relpath


def _apply_and_commit_groups(worktree_path: Path, study: str, fixes: list[FixEntry]) -> None:
    for (experiment, category), entries in _grouped_sorted(fixes):
        touched: set[str] = set()
        for entry in entries:
            touched.add(_apply_fix_to_worktree(worktree_path, study, entry.fix))
        _git(worktree_path, "add", "--", *sorted(touched))
        message = _commit_message(study, experiment, category, entries)
        _git(worktree_path, "commit", "-q", "-m", message)


# ---------------------------------------------------------------------------
# --dry-run preview
# ---------------------------------------------------------------------------


def _print_dry_run_preview(
    datasets_repo: Path, base: str, study: str, fixes: list[FixEntry]
) -> None:
    for (experiment, category), entries in _grouped_sorted(fixes):
        print(f"# group: experiment={experiment} category={category}")
        for entry in entries:
            fix = entry.fix
            relpath = _relpath_for_fix(study, fix)
            try:
                old_text = _show(datasets_repo, base, relpath)
            except subprocess.CalledProcessError as exc:
                print(f"!! {entry.discrepancy_id}: could not read {relpath}@{base}: {exc.stderr}")
                continue
            new_text = _compute_new_content(old_text, fix)
            print(f"--- {entry.discrepancy_id} ({fix.op}) {relpath}")
            diff = difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{relpath}",
                tofile=f"b/{relpath}",
            )
            sys.stdout.writelines(diff)
        print(f"  would commit: fix({study}/{experiment}): {_CATEGORY_SUMMARY[category]}")


# ---------------------------------------------------------------------------
# Lint regression check
# ---------------------------------------------------------------------------


def _pre_fix_error_count(run_dir: Path) -> int:
    lint_path = run_dir / "lint.json"
    if not lint_path.exists():
        print(
            f"warning: {lint_path} not found; treating pre-fix error count as 0",
            file=sys.stderr,
        )
        return 0
    report = schemas.LintReport.model_validate_json(lint_path.read_text())
    return sum(1 for f in report.findings if f.level == "error")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def apply_fixes(run_dir: Path, *, dry_run: bool = False, force_branch: bool = False) -> int:
    """Apply `run_dir/fix_plan.json`'s fixes to a fresh `git worktree` off
    the datasets repo, one commit per (experiment, category) group.

    Returns a process exit code (0 success/no-op, 1 refused/aborted). Never
    raises for expected failure modes (missing fix_plan, existing branch,
    lint regression, an unlocatable record) — each is reported to
    stderr/stdout and turned into a clean exit code instead.
    """
    fix_plan_path = run_dir / "fix_plan.json"
    if not fix_plan_path.exists():
        print(f"{fix_plan_path}: not found; run the fix-drafter agent first", file=sys.stderr)
        return 1
    fix_plan = schemas.FixPlan.model_validate_json(fix_plan_path.read_text())

    if not fix_plan.fixes:
        print("no auto-fixable discrepancies")
        return 0

    study, commit = rundir._parse_run_dir_name(run_dir)
    datasets_repo = config.datasets_repo()
    base = commit if commit is not None else _current_head(datasets_repo)
    branch = f"fix/{study}-structure"

    if dry_run:
        _print_dry_run_preview(datasets_repo, base, study, fix_plan.fixes)
        return 0

    if _branch_exists(datasets_repo, branch):
        if not force_branch:
            print(
                f"branch '{branch}' already exists; pass --force-branch to replace it",
                file=sys.stderr,
            )
            return 1
        _delete_branch(datasets_repo, branch)

    worktree_path = run_dir / ".worktree"
    _add_worktree(datasets_repo, worktree_path, branch, base)
    success = False
    try:
        try:
            _apply_and_commit_groups(worktree_path, study, fix_plan.fixes)
        except ApplyFixesError as exc:
            print(f"failed to apply fixes: {exc}; aborting", file=sys.stderr)
            return 1

        pre_fix_errors = _pre_fix_error_count(run_dir)
        post_fix_report = lint.lint_study(study, commit=None, repo_root=worktree_path)
        post_fix_errors = sum(1 for f in post_fix_report.findings if f.level == "error")

        if post_fix_errors > pre_fix_errors:
            print(
                f"lint regression: {post_fix_errors} error-level finding(s) after fixes "
                f"(was {pre_fix_errors} before); aborting, branch '{branch}' discarded",
                file=sys.stderr,
            )
            return 1

        print(f"applied {len(fix_plan.fixes)} fix(es) on branch '{branch}' (base {base})")
        success = True
        return 0
    finally:
        _remove_worktree(datasets_repo, worktree_path)
        if not success:
            _delete_branch(datasets_repo, branch)
