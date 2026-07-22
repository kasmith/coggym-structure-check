"""Tests for coggym_check.rundir — run-dir lifecycle, stage registry, and
the `init-run` CLI subcommand.

Design note exercised throughout: hashing an upstream artifact for
staleness detection only needs its *bytes*, not schema validity — a stage's
own artifact must validate against its schema (schemas.FILENAME_MODEL_MAP),
but an *input* artifact is hashed as-is. This lets these tests build minimal
fixture artifacts (e.g. a bare-bones valid `PaperStatus` for the "paper"
stage) without needing a full `StudySnapshot` for its "snapshot" input.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coggym_check import cli, config, rundir, schemas

STUDY = "TestStudy2020Mini"


@pytest.fixture()
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    d = tmp_path / "runs" / STUDY
    d.mkdir(parents=True)
    return d


def _write_lint(run_dir: Path) -> None:
    """Write a valid, input-free lint.json (the 'lint' stage has no inputs)."""
    payload = schemas.LintReport(study=STUDY, checks_run=["files-present"], findings=[])
    (run_dir / "lint.json").write_text(payload.model_dump_json(indent=2))


def _write_paper_status(run_dir: Path) -> None:
    """Write a valid paper_status.json (the 'paper' stage's own artifact)."""
    payload = schemas.PaperStatus(
        study=STUDY,
        status="found_local",
        pdf_path=f"studies/{STUDY}/paper.pdf",
        sha256="deadbeef",
        retrieved_from=None,
        candidates_tried=[],
        n_pages=10,
        text_quality=0.9,
        needs_direct_pdf_read=False,
        notes=[],
    )
    (run_dir / "paper_status.json").write_text(payload.model_dump_json(indent=2))


def _write_materials_manifest(run_dir: Path) -> None:
    payload = schemas.MaterialsManifest(
        study=STUDY, status="none_found", searches=[], sources=[], notes=[]
    )
    (run_dir / "materials_manifest.json").write_text(payload.model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# stage registry
# ---------------------------------------------------------------------------


def test_stage_registry_has_exact_names_and_order() -> None:
    """The registry's stage names/order are load-bearing (used later by the
    /check-study skill and headless runner) — pin them exactly."""
    assert list(rundir.STAGE_REGISTRY) == [
        "snapshot",
        "lint",
        "paper",
        "materials",
        "paper_summary",
        "materials_summary",
        "comparison",
        "fix",
        "render",
    ]
    assert rundir.STAGE_REGISTRY["snapshot"].artifact == "study_snapshot.json"
    assert rundir.STAGE_REGISTRY["snapshot"].inputs == ()
    assert rundir.STAGE_REGISTRY["lint"].artifact == "lint.json"
    assert rundir.STAGE_REGISTRY["lint"].inputs == ()
    assert rundir.STAGE_REGISTRY["paper"].artifact == "paper_status.json"
    assert rundir.STAGE_REGISTRY["paper"].inputs == ("snapshot",)
    assert rundir.STAGE_REGISTRY["materials"].artifact == "materials_manifest.json"
    assert rundir.STAGE_REGISTRY["materials"].inputs == ("snapshot", "paper")
    assert rundir.STAGE_REGISTRY["paper_summary"].artifact == "paper_summary.json"
    assert rundir.STAGE_REGISTRY["paper_summary"].inputs == ("snapshot", "paper")
    assert rundir.STAGE_REGISTRY["materials_summary"].artifact == "materials_summary.json"
    assert rundir.STAGE_REGISTRY["materials_summary"].inputs == ("materials",)
    assert rundir.STAGE_REGISTRY["comparison"].artifact == "comparison.json"
    assert rundir.STAGE_REGISTRY["comparison"].inputs == (
        "snapshot",
        "lint",
        "paper_summary",
        "materials_summary",
    )
    assert rundir.STAGE_REGISTRY["fix"].artifact == "fix_plan.json"
    assert rundir.STAGE_REGISTRY["fix"].inputs == ("comparison",)
    assert rundir.STAGE_REGISTRY["render"].artifact == "report.md"
    assert rundir.STAGE_REGISTRY["render"].inputs == ("comparison", "fix")


# ---------------------------------------------------------------------------
# stage_status: fresh dir
# ---------------------------------------------------------------------------


def test_stage_status_fresh_dir_all_missing(run_dir: Path) -> None:
    statuses = rundir.stage_status(run_dir)
    assert set(statuses) == set(rundir.STAGE_REGISTRY)
    assert all(status == "missing" for status in statuses.values())


# ---------------------------------------------------------------------------
# artifact-first semantics (no run_meta record needed for "done")
# ---------------------------------------------------------------------------


def test_stage_status_no_inputs_artifact_only_is_done(run_dir: Path) -> None:
    """`lint` has no inputs: a valid artifact with no run_meta record at all
    is 'done' (can never be stale, per the pre-answered clarification)."""
    _write_lint(run_dir)
    assert rundir.stage_status(run_dir)["lint"] == "done"


def test_stage_status_artifact_first_with_inputs_no_record_is_done(run_dir: Path) -> None:
    """`paper` has an input (snapshot), but with no run_meta record at all,
    a valid artifact is still 'done' — hash-checking only kicks in once a
    record exists (artifact-first semantics, pre-answered clarification)."""
    (run_dir / "study_snapshot.json").write_text('{"anything": "goes"}')
    _write_paper_status(run_dir)
    assert rundir.stage_status(run_dir)["paper"] == "done"


# ---------------------------------------------------------------------------
# record_stage + done via matching recorded hash
# ---------------------------------------------------------------------------


def test_record_stage_then_status_done_when_hash_matches(run_dir: Path) -> None:
    (run_dir / "study_snapshot.json").write_text('{"v": 1}')
    _write_paper_status(run_dir)

    rundir.record_stage(run_dir, "paper", "done")

    assert rundir.stage_status(run_dir)["paper"] == "done"
    run_meta_path = run_dir / "run_meta.json"
    assert run_meta_path.exists()
    data = json.loads(run_meta_path.read_text())
    schemas.RunMeta.model_validate(data)
    assert data["stages"]["paper"]["status"] == "done"
    assert "snapshot" in data["stages"]["paper"]["input_hashes"]


# ---------------------------------------------------------------------------
# staleness + propagation
# ---------------------------------------------------------------------------


def test_upstream_modification_makes_downstream_stale(run_dir: Path) -> None:
    (run_dir / "study_snapshot.json").write_text('{"v": 1}')
    _write_paper_status(run_dir)
    _write_materials_manifest(run_dir)

    rundir.record_stage(run_dir, "paper", "done")
    rundir.record_stage(run_dir, "materials", "done")

    assert rundir.stage_status(run_dir)["paper"] == "done"
    assert rundir.stage_status(run_dir)["materials"] == "done"

    # Modify the upstream artifact both "paper" and "materials" depend on.
    (run_dir / "study_snapshot.json").write_text('{"v": 2}')

    statuses = rundir.stage_status(run_dir)
    assert statuses["paper"] == "stale"
    assert statuses["materials"] == "stale"


def test_snapshot_stage_with_no_inputs_never_goes_stale(run_dir: Path) -> None:
    """A valid, unchanged study_snapshot.json is 'done' regardless of any
    other file churn, since 'snapshot' has no inputs to go stale against."""
    snapshot = schemas.StudySnapshot(
        study=STUDY,
        commit=None,
        generated_at="2026-01-01T00:00:00Z",
        tool_version="0.1.0",
        paper_pdf_present=False,
        source_url_csv=None,
        citation=schemas.Citation(),
        experiments={},
    )
    (run_dir / "study_snapshot.json").write_text(snapshot.model_dump_json(indent=2))
    rundir.record_stage(run_dir, "snapshot", "done")
    assert rundir.stage_status(run_dir)["snapshot"] == "done"

    # Churn some unrelated file; snapshot has no inputs so stays done.
    (run_dir / "lint.json").write_text('{"study": "x", "checks_run": [], "findings": []}')
    assert rundir.stage_status(run_dir)["snapshot"] == "done"


# ---------------------------------------------------------------------------
# skipped-with-reason surfaces
# ---------------------------------------------------------------------------


def test_skipped_stage_surfaces_status_and_reason(run_dir: Path) -> None:
    rundir.record_stage(
        run_dir, "materials", "skipped", skipped_reason="no OSF/GitHub link found"
    )
    assert rundir.stage_status(run_dir)["materials"] == "skipped"
    data = json.loads((run_dir / "run_meta.json").read_text())
    assert data["stages"]["materials"]["skipped_reason"] == "no OSF/GitHub link found"
    assert data["stages"]["materials"]["status"] == "skipped"


def test_failed_stage_surfaces_status(run_dir: Path) -> None:
    rundir.record_stage(run_dir, "paper", "failed")
    assert rundir.stage_status(run_dir)["paper"] == "failed"


# ---------------------------------------------------------------------------
# invalid artifact -> reported as missing, with a warning
# ---------------------------------------------------------------------------


def test_invalid_artifact_reported_as_missing_with_warning(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (run_dir / "lint.json").write_text('{"not": "a valid lint report"}')
    statuses = rundir.stage_status(run_dir)
    assert statuses["lint"] == "missing"
    err = capsys.readouterr().err
    assert "lint.json" in err
    assert "warning" in err.lower()


# ---------------------------------------------------------------------------
# run_dir_for / @shortsha naming
# ---------------------------------------------------------------------------


def test_run_dir_for_no_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    d = rundir.run_dir_for(STUDY, None)
    assert d == tmp_path / "runs" / STUDY
    assert d.is_dir()


def test_run_dir_for_with_commit_uses_shortsha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    sha = "0123456789abcdef"
    d = rundir.run_dir_for(STUDY, sha)
    assert d == tmp_path / "runs" / f"{STUDY}@{sha[:9]}"
    assert d.is_dir()


# ---------------------------------------------------------------------------
# init_run / init-run CLI
# ---------------------------------------------------------------------------


def test_init_run_creates_dir_and_run_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    d = rundir.init_run(STUDY)
    assert d == tmp_path / "runs" / STUDY
    run_meta_path = d / "run_meta.json"
    assert run_meta_path.exists()
    data = json.loads(run_meta_path.read_text())
    schemas.RunMeta.model_validate(data)
    assert data["study"] == STUDY
    assert data["stages"] == {}


def test_init_run_is_idempotent_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    d = rundir.init_run(STUDY)
    rundir.record_stage(d, "lint", "skipped", skipped_reason="test reason")

    # Calling init_run again must not clobber the recorded stage.
    rundir.init_run(STUDY)

    data = json.loads((d / "run_meta.json").read_text())
    assert data["stages"]["lint"]["status"] == "skipped"
    assert data["stages"]["lint"]["skipped_reason"] == "test reason"


def test_cli_init_run_prints_status_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["init-run", "AnyArbitraryStudyName"])

    assert exit_code == 0
    out = capsys.readouterr().out
    for stage in rundir.STAGE_REGISTRY:
        assert stage in out
    assert "missing" in out
    run_dir_path = tmp_path / "runs" / "AnyArbitraryStudyName"
    assert run_dir_path.is_dir()
    assert (run_dir_path / "run_meta.json").exists()


def test_cli_init_run_with_commit_uses_shortsha_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    sha = "abcdef0123456789"

    exit_code = cli.main(["init-run", "AnyStudy", "--commit", sha])

    assert exit_code == 0
    run_dir_path = tmp_path / "runs" / f"AnyStudy@{sha[:9]}"
    assert run_dir_path.is_dir()


# ---------------------------------------------------------------------------
# record-stage CLI subcommand (Finding 2, whole-branch review): gives
# interactive orchestration (which has no other way to call Python
# directly) a CLI entry point for the same record_stage bookkeeping
# headless.py already does in-process.
# ---------------------------------------------------------------------------


def test_cli_record_stage_done_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["record-stage", STUDY, "comparison", "done"])

    assert exit_code == 0
    run_dir = tmp_path / "runs" / STUDY
    out = capsys.readouterr().out.strip()
    assert Path(out) == run_dir
    data = json.loads((run_dir / "run_meta.json").read_text())
    assert data["stages"]["comparison"]["status"] == "done"


def test_cli_record_stage_skipped_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(
        ["record-stage", STUDY, "paper_summary", "skipped", "--reason", "paper_status is not_found"]
    )

    assert exit_code == 0
    run_dir = tmp_path / "runs" / STUDY
    data = json.loads((run_dir / "run_meta.json").read_text())
    assert data["stages"]["paper_summary"]["status"] == "skipped"
    assert data["stages"]["paper_summary"]["skipped_reason"] == "paper_status is not_found"


def test_cli_record_stage_with_commit_uses_shortsha_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    sha = "abcdef0123456789"

    exit_code = cli.main(["record-stage", STUDY, "fix", "failed", "--commit", sha])

    assert exit_code == 0
    run_dir = tmp_path / "runs" / f"{STUDY}@{sha[:9]}"
    data = json.loads((run_dir / "run_meta.json").read_text())
    assert data["stages"]["fix"]["status"] == "failed"


def test_cli_record_stage_invalid_stage_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["record-stage", STUDY, "not-a-real-stage", "done"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not-a-real-stage" in err


def test_cli_record_stage_invalid_status_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["record-stage", STUDY, "comparison", "not-a-real-status"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not-a-real-status" in err


def test_cli_record_stage_then_downstream_flips_stale_on_upstream_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The staleness flip the interactive skill's step 1/2 claims must now
    actually happen: record 'lint' via the CLI, write comparison.json as
    'done' too, then change lint.json -- comparison must flip to stale, and
    init-run's printed table must show it."""
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    run_dir = rundir.init_run(STUDY)
    _write_lint(run_dir)

    exit_code = cli.main(["record-stage", STUDY, "lint", "done"])
    assert exit_code == 0

    (run_dir / "study_snapshot.json").write_text('{"v": 1}')
    # 'comparison''s other two inputs (paper_summary, materials_summary) are
    # deliberately left absent -- _hash_artifact hashes a missing input as
    # the "missing" sentinel, so record_stage works fine without them.
    comparison = schemas.Comparison(
        study=STUDY,
        commit=None,
        inputs={},
        coverage=schemas.Coverage(paper="none", materials="none"),
        experiments=[],
        needs_human_judgment_count=0,
        clear_cut_count=0,
    )
    (run_dir / "comparison.json").write_text(comparison.model_dump_json(indent=2))
    cli.main(["record-stage", STUDY, "comparison", "done"])

    assert rundir.stage_status(run_dir)["comparison"] == "done"

    # Modify lint.json (an upstream input of 'comparison') after recording.
    _write_lint(run_dir)
    (run_dir / "lint.json").write_text(
        (run_dir / "lint.json").read_text().replace("files-present", "files-present-v2")
    )

    assert rundir.stage_status(run_dir)["comparison"] == "stale"

    capsys.readouterr()
    cli.main(["init-run", STUDY])
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip().startswith("comparison")]
    assert lines and "stale" in lines[0]
