"""Tests for coggym_check.fixer — the safety-critical `apply-fixes` path.

Every test here exercises `fixer.apply_fixes` against a throwaway datasets
repo built by the `datasets_repo_factory` fixture (tests/conftest.py):
detached HEAD, an untracked scratch file, one commit holding the
`TestStudy2020Mini` fixture. The one invariant every single test asserts,
one way or another, is constraints.md hard rule #3: the main working
tree/index/HEAD of that repo is never touched — only `git worktree` is.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import pytest

from coggym_check import config, fixer, lint, schemas

STUDY = "TestStudy2020Mini"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def _repo_status(repo_root: Path) -> tuple[str, str]:
    """(porcelain status, HEAD sha) -- the "did fixer.py touch the main
    working tree/HEAD" fingerprint every test compares before/after."""
    status = _git(repo_root, "status", "--porcelain")
    head = _git(repo_root, "rev-parse", "HEAD").strip()
    return status, head


def _write_fix_plan(run_dir: Path, fixes: list[schemas.FixEntry], dropped=None) -> None:
    plan = schemas.FixPlan(
        study=STUDY,
        fixes=fixes,
        dropped=dropped or [],
        pr_title=f"Fix structure discrepancies in {STUDY}",
        pr_body="test fixture fix plan",
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "fix_plan.json").write_text(plan.model_dump_json(indent=2))


def _write_lint_baseline(run_dir: Path, repo_root: Path) -> None:
    """Write the *real* pre-fix lint.json for `repo_root`'s current state.

    The Mini fixture's trials reference assets/*.mp4 that don't exist on
    disk (test_lint.py's docstring explains why: Task 3 built Mini without
    real media on purpose), so it always has a nonzero baseline error count
    from the assets-exist check -- hand-writing a "clean" lint.json here
    would make every test see a false lint regression the moment fixer.py
    re-lints the worktree. Computing the real baseline is the only way the
    regression check (fixer.py's actual job) gets exercised correctly.
    """
    report = lint.lint_study(STUDY, repo_root=repo_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "lint.json").write_text(report.model_dump_json(indent=2))


def _instruction_text_fix(discrepancy_id: str = "d1") -> schemas.FixEntry:
    return schemas.FixEntry(
        fix=schemas.SetInstructionText(
            op="set_instruction_text",
            experiment="exp1",
            record_id="instruction_intro",
            new_value="Welcome to the study. You will watch three short clips.",
        ),
        discrepancy_id=discrepancy_id,
        commit_message=f"fix({STUDY}/exp1): correct instruction text to match materials",
    )


@pytest.fixture()
def repo(datasets_repo_factory, monkeypatch: pytest.MonkeyPatch):
    fake = datasets_repo_factory()
    monkeypatch.setattr(config, "datasets_repo", lambda: fake.root)
    return fake


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs" / STUDY
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# no-op paths
# ---------------------------------------------------------------------------


def test_empty_fix_plan_writes_nothing_and_exits_0(repo, run_dir, capsys) -> None:
    _write_fix_plan(run_dir, [])
    before = _repo_status(repo.root)

    code = fixer.apply_fixes(run_dir)

    assert code == 0
    assert "no auto-fixable" in capsys.readouterr().out
    assert not (run_dir / ".worktree").exists()
    assert _repo_status(repo.root) == before
    assert not fixer._branch_exists(repo.root, f"fix/{STUDY}-structure")


def test_missing_fix_plan_returns_1(repo, run_dir) -> None:
    code = fixer.apply_fixes(run_dir)
    assert code == 1


# ---------------------------------------------------------------------------
# never-push (review, not just a grep, but the grep is cheap insurance)
# ---------------------------------------------------------------------------


def test_module_never_calls_git_push() -> None:
    """No `git ... push ...` subprocess call anywhere in fixer.py.

    Checked as a quoted CLI argument (`"push"`/`'push'`), not a bare
    substring match, since the module's own docstring discusses this
    invariant in prose ("`git push` never appears...") using the word
    itself.
    """
    source = inspect.getsource(fixer)
    assert '"push"' not in source
    assert "'push'" not in source


# ---------------------------------------------------------------------------
# main-working-tree/HEAD/untracked-file preservation + branch/worktree lifecycle
# ---------------------------------------------------------------------------


def test_applies_fix_leaves_main_worktree_and_head_untouched(repo, run_dir) -> None:
    _write_fix_plan(run_dir, [_instruction_text_fix()])
    _write_lint_baseline(run_dir, repo.root)
    scratch_before = (repo.root / "_scratch_untracked.txt").read_bytes()
    before = _repo_status(repo.root)

    code = fixer.apply_fixes(run_dir)

    assert code == 0
    assert _repo_status(repo.root) == before
    assert (repo.root / "_scratch_untracked.txt").read_bytes() == scratch_before
    assert not (run_dir / ".worktree").exists()

    branch = f"fix/{STUDY}-structure"
    assert fixer._branch_exists(repo.root, branch)

    # base is unchanged: the branch's parent is exactly the pre-fix HEAD.
    parent = _git(repo.root, "rev-parse", f"{branch}~1").strip()
    assert parent == repo.head_sha


def test_worktree_removed_and_pruned_after_success(repo, run_dir) -> None:
    _write_fix_plan(run_dir, [_instruction_text_fix()])
    _write_lint_baseline(run_dir, repo.root)

    fixer.apply_fixes(run_dir)

    assert not (run_dir / ".worktree").exists()
    listing = _git(repo.root, "worktree", "list", "--porcelain")
    assert ".worktree" not in listing


# ---------------------------------------------------------------------------
# each FixOp type produces exactly the intended change
# ---------------------------------------------------------------------------


def test_set_instruction_text_changes_only_target_record(repo, run_dir) -> None:
    orig_lines = (
        (repo.root / "studies" / STUDY / "exp1" / "instruction.jsonl").read_text().splitlines()
    )
    _write_fix_plan(run_dir, [_instruction_text_fix()])
    _write_lint_baseline(run_dir, repo.root)

    assert fixer.apply_fixes(run_dir) == 0

    branch = f"fix/{STUDY}-structure"
    new_text = _git(
        repo.root, "show", f"{branch}:studies/{STUDY}/exp1/instruction.jsonl"
    )
    new_lines = new_text.splitlines()
    assert len(new_lines) == len(orig_lines)

    changed = json.loads(new_lines[0])
    assert changed["id"] == "instruction_intro"
    assert changed["text"] == "Welcome to the study. You will watch three short clips."
    # every other line byte-identical to the original
    assert new_lines[1:] == orig_lines[1:]
    # the changed line's serialization uses the same separators style as the
    # rest of the file (", ", ": ") -- not the default json.dumps compact.
    assert '", "' in new_lines[0] or ", " in new_lines[0]


def test_set_config_field_dotted_scalar_path(repo, run_dir) -> None:
    fix = schemas.FixEntry(
        fix=schemas.SetConfigField(
            op="set_config_field",
            experiment="exp2",
            key_path="paperDOI",
            new_value="10.1000/test-correct",
        ),
        discrepancy_id="d4",
        commit_message=f"fix({STUDY}/exp2): correct citation DOI",
    )
    _write_fix_plan(run_dir, [fix])
    _write_lint_baseline(run_dir, repo.root)

    assert fixer.apply_fixes(run_dir) == 0

    branch = f"fix/{STUDY}-structure"
    new_config = json.loads(_git(repo.root, "show", f"{branch}:studies/{STUDY}/exp2/config.json"))
    orig_config = json.loads((repo.root / "studies" / STUDY / "exp2" / "config.json").read_text())

    assert new_config["paperDOI"] == "10.1000/test-correct"
    # every other key unchanged
    for key in orig_config:
        if key != "paperDOI":
            assert new_config[key] == orig_config[key]
    # key order preserved
    assert list(new_config.keys()) == list(orig_config.keys())


def test_set_config_field_list_index_segment(repo, run_dir) -> None:
    """Integer key_path segments index lists (task-8 pre-answered
    clarification) -- exercised generically via set_config_field, distinct
    from the dedicated set_block_randomization op."""
    fix = schemas.FixEntry(
        fix=schemas.SetConfigField(
            op="set_config_field",
            experiment="exp1",
            key_path="experimentFlow.0.experimental_condition",
            new_value="cond1_renamed",
        ),
        discrepancy_id="d5",
        commit_message=f"fix({STUDY}/exp1): rename condition",
    )
    _write_fix_plan(run_dir, [fix])
    _write_lint_baseline(run_dir, repo.root)

    assert fixer.apply_fixes(run_dir) == 0

    branch = f"fix/{STUDY}-structure"
    new_config = json.loads(_git(repo.root, "show", f"{branch}:studies/{STUDY}/exp1/config.json"))
    assert new_config["experimentFlow"][0]["experimental_condition"] == "cond1_renamed"


def test_set_block_randomization_changes_only_target_entry(repo, run_dir) -> None:
    fix = schemas.FixEntry(
        fix=schemas.SetBlockRandomization(
            op="set_block_randomization",
            experiment="exp1",
            condition_index=0,
            block_index=1,
            new_value=True,
        ),
        discrepancy_id="d2",
        commit_message=f"fix({STUDY}/exp1): correct block randomization flag",
    )
    _write_fix_plan(run_dir, [fix])
    _write_lint_baseline(run_dir, repo.root)

    assert fixer.apply_fixes(run_dir) == 0

    branch = f"fix/{STUDY}-structure"
    new_config = json.loads(_git(repo.root, "show", f"{branch}:studies/{STUDY}/exp1/config.json"))
    orig_config = json.loads((repo.root / "studies" / STUDY / "exp1" / "config.json").read_text())

    new_br = new_config["experimentFlow"][0]["block_randomization"]
    orig_br = orig_config["experimentFlow"][0]["block_randomization"]
    assert new_br == [orig_br[0], True]
    assert new_br[0] == orig_br[0]


def test_set_slider_labels_changes_only_target_trial_and_query(repo, run_dir) -> None:
    orig_lines = (
        (repo.root / "studies" / STUDY / "exp1" / "trial.jsonl").read_text().splitlines()
    )
    fix = schemas.FixEntry(
        fix=schemas.SetSliderLabels(
            op="set_slider_labels",
            experiment="exp1",
            trial_id="scenario_001",
            query_tag="truck_preference",
            new_labels=[{"value": 0, "label": "none"}, {"value": 10, "label": "a lot"}],
            new_min=0,
            new_max=10,
        ),
        discrepancy_id="d6",
        commit_message=f"fix({STUDY}/exp1): correct slider labels",
    )
    _write_fix_plan(run_dir, [fix])
    _write_lint_baseline(run_dir, repo.root)

    assert fixer.apply_fixes(run_dir) == 0

    branch = f"fix/{STUDY}-structure"
    new_text = _git(repo.root, "show", f"{branch}:studies/{STUDY}/exp1/trial.jsonl")
    new_lines = new_text.splitlines()
    assert len(new_lines) == len(orig_lines)

    changed = json.loads(new_lines[0])
    assert changed["id"] == "scenario_001"
    truck_query = next(q for q in changed["queries"] if q["tag"] == "truck_preference")
    assert truck_query["slider_config"]["min"] == 0
    assert truck_query["slider_config"]["max"] == 10
    assert truck_query["slider_config"]["labels"] == [
        {"value": 0, "label": "none"},
        {"value": 10, "label": "a lot"},
    ]
    # the other query on the same trial line is untouched
    belief_query = next(q for q in changed["queries"] if q["tag"] == "belief_at_hidden")
    orig_first = json.loads(orig_lines[0])
    orig_belief = next(q for q in orig_first["queries"] if q["tag"] == "belief_at_hidden")
    assert belief_query == orig_belief

    # every other trial line byte-identical to the original
    assert new_lines[1:] == orig_lines[1:]


# ---------------------------------------------------------------------------
# commit grouping: one commit per (experiment, category), sorted order
# ---------------------------------------------------------------------------


def test_multiple_categories_produce_separate_sorted_commits(repo, run_dir) -> None:
    instruction_fix = _instruction_text_fix()
    citation_fix = schemas.FixEntry(
        fix=schemas.SetConfigField(
            op="set_config_field",
            experiment="exp2",
            key_path="paperDOI",
            new_value="10.1000/test-correct",
        ),
        discrepancy_id="d4",
        commit_message=f"fix({STUDY}/exp2): correct citation DOI",
    )
    _write_fix_plan(run_dir, [citation_fix, instruction_fix])
    _write_lint_baseline(run_dir, repo.root)

    assert fixer.apply_fixes(run_dir) == 0

    branch = f"fix/{STUDY}-structure"
    log = _git(repo.root, "log", "--reverse", "--format=%s", f"{repo.head_sha}..{branch}")
    subjects = [line for line in log.splitlines() if line]

    assert len(subjects) == 2
    # sorted by (experiment, category): "exp1" < "exp2"
    assert subjects[0] == f"fix({STUDY}/exp1): update instruction text to match verbatim materials"
    assert subjects[1] == f"fix({STUDY}/exp2): correct citation metadata"

    body = _git(repo.root, "log", "-1", "--format=%b", branch)
    assert "d4" in body


# ---------------------------------------------------------------------------
# existing-branch refusal / --force-branch
# ---------------------------------------------------------------------------


def test_existing_branch_refused_without_force(repo, run_dir) -> None:
    branch = f"fix/{STUDY}-structure"
    _git(repo.root, "branch", branch, repo.head_sha)
    before_target = _git(repo.root, "rev-parse", branch).strip()

    _write_fix_plan(run_dir, [_instruction_text_fix()])
    _write_lint_baseline(run_dir, repo.root)
    before = _repo_status(repo.root)

    code = fixer.apply_fixes(run_dir)

    assert code == 1
    assert _git(repo.root, "rev-parse", branch).strip() == before_target
    assert _repo_status(repo.root) == before
    assert not (run_dir / ".worktree").exists()


def test_force_branch_deletes_and_recreates(repo, run_dir) -> None:
    branch = f"fix/{STUDY}-structure"
    _git(repo.root, "branch", branch, repo.head_sha)
    stale_target = _git(repo.root, "rev-parse", branch).strip()

    _write_fix_plan(run_dir, [_instruction_text_fix()])
    _write_lint_baseline(run_dir, repo.root)

    code = fixer.apply_fixes(run_dir, force_branch=True)

    assert code == 0
    new_target = _git(repo.root, "rev-parse", branch).strip()
    assert new_target != stale_target
    parent = _git(repo.root, "rev-parse", f"{branch}~1").strip()
    assert parent == repo.head_sha


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_creates_no_branch_or_worktree(repo, run_dir, capsys) -> None:
    _write_fix_plan(run_dir, [_instruction_text_fix()])
    _write_lint_baseline(run_dir, repo.root)
    before = _repo_status(repo.root)

    code = fixer.apply_fixes(run_dir, dry_run=True)

    assert code == 0
    out = capsys.readouterr().out
    assert "d1" in out
    assert "instruction.jsonl" in out
    assert not (run_dir / ".worktree").exists()
    assert not fixer._branch_exists(repo.root, f"fix/{STUDY}-structure")
    assert _repo_status(repo.root) == before


# ---------------------------------------------------------------------------
# lint-regression abort
# ---------------------------------------------------------------------------


def test_lint_regression_aborts_deletes_branch_leaves_repo_clean(repo, run_dir) -> None:
    # min > max triggers lint.py's slider-sane error-level check.
    fix = schemas.FixEntry(
        fix=schemas.SetSliderLabels(
            op="set_slider_labels",
            experiment="exp1",
            trial_id="scenario_001",
            query_tag="truck_preference",
            new_labels=None,
            new_min=100,
            new_max=1,
        ),
        discrepancy_id="d7",
        commit_message=f"fix({STUDY}/exp1): (deliberately bad) slider range",
    )
    _write_fix_plan(run_dir, [fix])
    _write_lint_baseline(run_dir, repo.root)
    before = _repo_status(repo.root)

    code = fixer.apply_fixes(run_dir)

    assert code == 1
    branch = f"fix/{STUDY}-structure"
    assert not fixer._branch_exists(repo.root, branch)
    assert not (run_dir / ".worktree").exists()
    assert _repo_status(repo.root) == before
    listing = _git(repo.root, "worktree", "list", "--porcelain")
    assert ".worktree" not in listing


# ---------------------------------------------------------------------------
# pinned-commit base resolution
# ---------------------------------------------------------------------------


def test_pinned_commit_run_dir_uses_pinned_base_not_current_head(
    repo, tmp_path: Path
) -> None:
    pinned_sha = repo.head_sha
    # advance the fake datasets repo past the pinned commit, so a bug that
    # used "current HEAD" instead of the pin would be caught.
    (repo.root / "studies" / STUDY / "exp1" / "README.md").write_text("advanced\n")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-q", "-m", "advance past the pin")
    _git(repo.root, "checkout", "-q", "--detach", "HEAD")

    pinned_run_dir = tmp_path / "runs" / f"{STUDY}@{pinned_sha[:9]}"
    pinned_run_dir.mkdir(parents=True)
    _write_fix_plan(pinned_run_dir, [_instruction_text_fix()])
    _write_lint_baseline(pinned_run_dir, repo.root)

    code = fixer.apply_fixes(pinned_run_dir)

    assert code == 0
    branch = f"fix/{STUDY}-structure"
    parent = _git(repo.root, "rev-parse", f"{branch}~1").strip()
    assert parent == pinned_sha
