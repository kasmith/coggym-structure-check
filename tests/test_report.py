"""Golden-file tests for coggym_check.report — report.md/pr.md rendering.

`render()` is pure (comparison/fix_plan/paper_status/materials_manifest
models in, `generated_at` passed explicitly, `(report_md, pr_md)` strings
out — see report.py's module docstring), so these tests load committed
fixture artifacts, call `render()` with a fixed `generated_at`, and assert
byte-exact equality against committed golden `.md` files. Any intentional
formatting change to report.py must regenerate these goldens by hand (and
a reviewer should read the diff) rather than the test auto-updating them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coggym_check import cli, config, report, schemas

FIXTURES = Path(__file__).parent / "fixtures" / "artifacts"
GENERATED_AT = "2026-01-15T12:00:00Z"


def _load(dirname: str, filename: str, model):
    path = FIXTURES / dirname / filename
    if not path.exists():
        return None
    return model.model_validate_json(path.read_text())


def test_render_full_artifacts_matches_golden() -> None:
    """Every optional artifact present: coverage caveat (materials partial),
    both auto-fixed and needs-human-judgment tables, a declined fix, and a
    non-empty evidence appendix."""
    comparison = schemas.Comparison.model_validate_json(
        (FIXTURES / "report" / "comparison.json").read_text()
    )
    fix_plan = _load("report", "fix_plan.json", schemas.FixPlan)
    paper_status = _load("report", "paper_status.json", schemas.PaperStatus)
    materials_manifest = _load("report", "materials_manifest.json", schemas.MaterialsManifest)

    report_md, pr_md = report.render(
        comparison, fix_plan, paper_status, materials_manifest, GENERATED_AT
    )

    expected_report = (FIXTURES / "report" / "report.md").read_text()
    expected_pr = (FIXTURES / "report" / "pr.md").read_text()

    assert report_md == expected_report
    assert pr_md == expected_pr


def test_render_degraded_no_optional_artifacts_matches_golden() -> None:
    """fix_plan/paper_status/materials_manifest all absent, coverage none/none:
    every "not available" note must appear, and the PR body must fall back
    to the fix_plan-unavailable note while the needs-human-judgment
    checklist still renders from comparison.json alone."""
    comparison = schemas.Comparison.model_validate_json(
        (FIXTURES / "report_degraded" / "comparison.json").read_text()
    )

    report_md, pr_md = report.render(comparison, None, None, None, GENERATED_AT)

    expected_report = (FIXTURES / "report_degraded" / "report.md").read_text()
    expected_pr = (FIXTURES / "report_degraded" / "pr.md").read_text()

    assert report_md == expected_report
    assert pr_md == expected_pr


def test_render_returns_tuple_of_two_strings() -> None:
    comparison = schemas.Comparison.model_validate_json(
        (FIXTURES / "report" / "comparison.json").read_text()
    )
    result = report.render(comparison, None, None, None, GENERATED_AT)
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert all(isinstance(part, str) for part in result)


# ---------------------------------------------------------------------------
# CLI: render subcommand
# ---------------------------------------------------------------------------


def test_cli_render_records_stage_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2 (whole-branch review): `render` must record its stage
    'done' once report.md/pr.md are written, mirroring
    headless.run_fix_and_render_stage's render half."""
    study = "TestStudy2020Mini"
    valid_dir = Path(__file__).parent / "fixtures" / "artifacts" / "valid"
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    run_dir = tmp_path / "runs" / study
    run_dir.mkdir(parents=True)
    (run_dir / "comparison.json").write_text((valid_dir / "comparison.json").read_text())

    exit_code = cli.main(["render", study])

    assert exit_code == 0
    assert (run_dir / "report.md").exists()
    assert (run_dir / "pr.md").exists()
    run_meta = json.loads((run_dir / "run_meta.json").read_text())
    assert run_meta["stages"]["render"]["status"] == "done"
