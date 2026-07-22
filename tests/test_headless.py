"""Tests for coggym_check.headless — the headless batch runner (`python -m
coggym_check run`) and its `run` CLI wiring.

No real `claude` invocations anywhere (constraints.md hard rule #8: no LLM
calls in unit tests): every agentic-stage test monkeypatches
`headless.subprocess.run` with a canned `claude -p --output-format json`
JSON stdout (see `headless._parse_claude_output`'s docstring for the
assumed shape) and, where the test needs a stage to "succeed", writes the
stage's own artifact to disk itself first — exactly what a real `claude`
subprocess would have done via its own `Write`/`Bash` tools before exiting.

Agent-file parsing tests round-trip against the real
`.claude/agents/*.md` files (not fixtures) — those six files are the
actual contract this parser must handle, quoted-colon description and
all (`paper-finder.md`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coggym_check import cli, config, dataset, headless, lint, rundir, schemas

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
FIXTURES = Path(__file__).parent / "fixtures"

#: Exact tools/model per real agent file — pinned so a round-trip test
#: catches any drift in the checked-in frontmatter, not just "parses without
#: crashing."
_EXPECTED_AGENTS: dict[str, tuple[list[str], str]] = {
    "fix-drafter": (["Read", "Write", "Bash"], "sonnet"),
    "materials-analyst": (["Read", "Grep", "Glob", "Write", "Bash"], "sonnet"),
    "materials-scout": (
        ["WebSearch", "WebFetch", "Read", "Grep", "Write", "Bash"],
        "sonnet",
    ),
    "paper-analyst": (["Read", "Grep", "Glob", "Write", "Bash"], "opus"),
    "paper-finder": (["WebSearch", "WebFetch", "Bash", "Read", "Write"], "sonnet"),
    "structure-comparator": (["Read", "Grep", "Write", "Bash"], "opus"),
}


# ---------------------------------------------------------------------------
# parse_agent_file — round trip against all six real agent files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_EXPECTED_AGENTS))
def test_parse_agent_file_round_trip(name: str) -> None:
    expected_tools, expected_model = _EXPECTED_AGENTS[name]
    spec = headless.parse_agent_file(AGENTS_DIR / f"{name}.md")

    assert spec.name == name
    assert spec.model == expected_model
    assert spec.tools == expected_tools
    assert spec.description.strip()
    # The one file with a quoted description (paper-finder.md) must come
    # back unwrapped, not carrying its literal surrounding quote marks.
    assert not spec.description.startswith('"')
    assert not spec.description.endswith('"')

    # Body must be non-empty and must not leak any frontmatter line.
    assert spec.body.strip()
    assert spec.body.strip().startswith("## Mission")
    assert "\nname:" not in spec.body
    assert "\ntools:" not in spec.body
    assert "\nmodel:" not in spec.body
    assert not spec.body.lstrip().startswith("---")


def test_parse_agent_file_paper_finder_description_quoted_colon_safe() -> None:
    """paper-finder.md's description is the one wrapped in quotes; the raw
    file's frontmatter line still has ':' as its first colon (the key/value
    separator) — assert the parser split on that first colon only, not on
    the substance of the (quoted) value."""
    spec = headless.parse_agent_file(AGENTS_DIR / "paper-finder.md")
    assert spec.name == "paper-finder"
    assert "extract-paper" in spec.description
    assert "not_found" in spec.description


# ---------------------------------------------------------------------------
# claude CLI command construction
# ---------------------------------------------------------------------------


def test_build_claude_command_exact_argv() -> None:
    agent = headless.AgentSpec(
        name="x", description="d", tools=["Read", "Write"], model="sonnet", body="BODY TEXT"
    )
    cmd = headless.build_claude_command(agent, "PROMPT TEXT", 42)
    assert cmd == [
        "claude",
        "-p",
        "PROMPT TEXT",
        "--append-system-prompt",
        "BODY TEXT",
        "--allowedTools",
        "Read,Write",
        "--model",
        "sonnet",
        "--output-format",
        "json",
        "--max-turns",
        "42",
    ]


def test_build_stage_prompt_mentions_all_required_pieces(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "SomeStudy"
    prompt = headless.build_stage_prompt(
        "SomeStudy",
        run_dir,
        [run_dir / "study_snapshot.json", run_dir / "lint.json"],
        run_dir / "comparison.json",
    )
    assert "SomeStudy" in prompt
    assert str(run_dir) in prompt
    assert str(run_dir / "study_snapshot.json") in prompt
    assert str(run_dir / "lint.json") in prompt
    assert str(run_dir / "comparison.json") in prompt
    assert "validate-artifact" in prompt


# ---------------------------------------------------------------------------
# claude JSON output parsing — tolerant of missing fields
# ---------------------------------------------------------------------------


def test_parse_claude_output_full_shape() -> None:
    stdout = json.dumps(
        {
            "result": "done",
            "total_cost_usd": 0.0512,
            "duration_ms": 1500,
            "session_id": "sess-123",
        }
    )
    meta = headless._parse_claude_output(stdout)
    assert meta.cost_usd == pytest.approx(0.0512)
    assert meta.duration_s == pytest.approx(1.5)
    assert meta.session_id == "sess-123"


def test_parse_claude_output_missing_fields_tolerant() -> None:
    meta = headless._parse_claude_output(json.dumps({"result": "done"}))
    assert meta.cost_usd == 0.0
    assert meta.duration_s == 0.0
    assert meta.session_id == ""


def test_parse_claude_output_invalid_json_tolerant() -> None:
    meta = headless._parse_claude_output("not valid json at all")
    assert meta.cost_usd == 0.0
    assert meta.session_id == ""


# ---------------------------------------------------------------------------
# run_agentic_stage — mocked subprocess, real record_stage bookkeeping
# ---------------------------------------------------------------------------


def _valid_comparison_json(study: str) -> str:
    payload = schemas.Comparison(
        study=study,
        commit=None,
        inputs={},
        coverage=schemas.Coverage(paper="none", materials="none"),
        experiments=[],
        needs_human_judgment_count=0,
        clear_cut_count=0,
    )
    return payload.model_dump_json(indent=2)


@pytest.fixture()
def runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    return tmp_path / "runs"


def test_run_agentic_stage_success_records_done_with_claude_meta(
    runs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = rundir.init_run("SomeStudy")
    canned_stdout = json.dumps(
        {"total_cost_usd": 0.1234, "duration_ms": 2500, "session_id": "sess-abc"}
    )

    def fake_run(cmd: list[str], capture_output: bool, text: bool) -> subprocess.CompletedProcess:
        # Simulate the real `claude` subprocess writing its artifact via its
        # own tools before exiting.
        (run_dir / "comparison.json").write_text(_valid_comparison_json("SomeStudy"))
        return subprocess.CompletedProcess(cmd, 0, stdout=canned_stdout, stderr="")

    monkeypatch.setattr(headless.subprocess, "run", fake_run)

    headless.run_agentic_stage(run_dir, "SomeStudy", "comparison", "structure-comparator", 60)

    assert rundir.stage_status(run_dir)["comparison"] == "done"
    data = json.loads((run_dir / "run_meta.json").read_text())
    claude_meta = data["stages"]["comparison"]["claude"]
    assert claude_meta is not None
    assert claude_meta["cost_usd"] == pytest.approx(0.1234)
    assert claude_meta["duration_s"] == pytest.approx(2.5)
    assert claude_meta["session_id"] == "sess-abc"


def test_run_agentic_stage_invalid_artifact_marks_failed_not_raises(
    runs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = rundir.init_run("SomeStudy2")
    canned_stdout = json.dumps(
        {"total_cost_usd": 0.01, "duration_ms": 100, "session_id": "sess-xyz"}
    )

    def fake_run(cmd: list[str], capture_output: bool, text: bool) -> subprocess.CompletedProcess:
        # Never actually writes comparison.json — simulates the agent
        # claiming success without producing a valid artifact.
        return subprocess.CompletedProcess(cmd, 0, stdout=canned_stdout, stderr="")

    monkeypatch.setattr(headless.subprocess, "run", fake_run)

    # Must not raise: stage failure is recorded, not propagated as an
    # exception, so the caller can move on to the next stage/study.
    headless.run_agentic_stage(run_dir, "SomeStudy2", "comparison", "structure-comparator", 60)

    assert rundir.stage_status(run_dir)["comparison"] == "failed"
    data = json.loads((run_dir / "run_meta.json").read_text())
    assert data["stages"]["comparison"]["status"] == "failed"
    # Claude metadata is still recorded even on a failed stage.
    assert data["stages"]["comparison"]["claude"]["session_id"] == "sess-xyz"


# ---------------------------------------------------------------------------
# skip-rule behavior
# ---------------------------------------------------------------------------


def test_paper_summary_skipped_when_paper_status_not_found(
    runs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = rundir.init_run("SkipStudy")
    paper_status = schemas.PaperStatus(
        study="SkipStudy",
        status="not_found",
        pdf_path=None,
        sha256=None,
        retrieved_from=None,
        candidates_tried=[],
        n_pages=None,
        text_quality=None,
        needs_direct_pdf_read=True,
        notes=[],
    )
    (run_dir / "paper_status.json").write_text(paper_status.model_dump_json(indent=2))

    called: list[object] = []
    monkeypatch.setattr(headless, "run_agentic_stage", lambda *a, **k: called.append(a))

    headless.run_paper_summary_stage(run_dir, "SkipStudy", 60)

    assert called == []
    assert rundir.stage_status(run_dir)["paper_summary"] == "skipped"
    data = json.loads((run_dir / "run_meta.json").read_text())
    assert "not_found" in data["stages"]["paper_summary"]["skipped_reason"]


def test_paper_summary_runs_when_paper_status_found_local(
    runs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = rundir.init_run("RunStudy")
    paper_status = schemas.PaperStatus(
        study="RunStudy",
        status="found_local",
        pdf_path="materials/RunStudy/paper.pdf",
        sha256="deadbeef",
        retrieved_from="studies/RunStudy/paper.pdf",
        candidates_tried=[],
        n_pages=5,
        text_quality=1.0,
        needs_direct_pdf_read=False,
        notes=[],
    )
    (run_dir / "paper_status.json").write_text(paper_status.model_dump_json(indent=2))

    called: list[object] = []
    monkeypatch.setattr(headless, "run_agentic_stage", lambda *a, **k: called.append(a))

    headless.run_paper_summary_stage(run_dir, "RunStudy", 60)

    assert len(called) == 1


def test_materials_summary_skipped_when_manifest_none_found(
    runs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = rundir.init_run("SkipMaterials")
    manifest = schemas.MaterialsManifest(
        study="SkipMaterials", status="none_found", searches=[], sources=[], notes=[]
    )
    (run_dir / "materials_manifest.json").write_text(manifest.model_dump_json(indent=2))

    called: list[object] = []
    monkeypatch.setattr(headless, "run_agentic_stage", lambda *a, **k: called.append(a))

    headless.run_materials_summary_stage(run_dir, "SkipMaterials", 60)

    assert called == []
    assert rundir.stage_status(run_dir)["materials_summary"] == "skipped"
    data = json.loads((run_dir / "run_meta.json").read_text())
    assert "none_found" in data["stages"]["materials_summary"]["skipped_reason"]


# ---------------------------------------------------------------------------
# run_study: deterministic stages for real, agentic stages mocked, and the
# pipeline keeps going past a failed agentic stage.
# ---------------------------------------------------------------------------


def test_run_study_continues_past_agentic_stage_failure(
    runs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = schemas.StudySnapshot(
        study="S",
        commit=None,
        generated_at="2026-01-01T00:00:00Z",
        tool_version="0.1.0",
        paper_pdf_present=False,
        source_url_csv=None,
        citation=schemas.Citation(),
        experiments={},
    )
    lint_report = schemas.LintReport(study="S", checks_run=[], findings=[])
    paper_status = schemas.PaperStatus(
        study="S",
        status="found_local",
        pdf_path="p",
        sha256="h",
        retrieved_from=None,
        candidates_tried=[],
        n_pages=1,
        text_quality=1.0,
        needs_direct_pdf_read=False,
        notes=[],
    )

    monkeypatch.setattr(headless.dataset, "load_study", lambda study, commit=None: snapshot)
    monkeypatch.setattr(headless.lint, "lint_study", lambda study, commit=None: lint_report)
    monkeypatch.setattr(headless.paper, "extract_paper", lambda study: paper_status)

    calls: list[str] = []

    def fake_agentic(run_dir: Path, study: str, stage: str, agent_name: str, max_turns: int) -> None:
        calls.append(stage)
        rundir.record_stage(run_dir, stage, "failed")

    def fake_fix_render(run_dir: Path, study: str, max_turns: int) -> None:
        calls.append("fix")
        rundir.record_stage(run_dir, "fix", "failed")
        rundir.record_stage(run_dir, "render", "failed")

    monkeypatch.setattr(headless, "run_agentic_stage", fake_agentic)
    monkeypatch.setattr(headless, "run_fix_and_render_stage", fake_fix_render)

    run_dir = headless.run_study("S")

    # Every downstream stage was still attempted, in order, even though
    # every agentic one "failed" — the loop never stopped early.
    assert calls == ["materials", "paper_summary", "materials_summary", "comparison", "fix"]

    statuses = rundir.stage_status(run_dir)
    assert statuses["snapshot"] == "done"
    assert statuses["lint"] == "done"
    assert statuses["paper"] == "done"
    assert statuses["comparison"] == "failed"
    assert statuses["fix"] == "failed"
    assert statuses["render"] == "failed"


def test_run_snapshot_stage_raises_fatal_on_missing_study(
    runs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "datasets_repo", lambda: FIXTURES)
    run_dir = rundir.init_run("NoSuchStudyAtAll")
    with pytest.raises(headless.StudyFatalError):
        headless.run_snapshot_stage(run_dir, "NoSuchStudyAtAll", None)


# ---------------------------------------------------------------------------
# run(): sequential multi-study loop, one fatal error doesn't stop the next
# ---------------------------------------------------------------------------


def test_run_studies_sequential_fatal_error_does_not_stop_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_run_study(
        study: str, *, commit: str | None = None, force: bool = False, max_turns: int = 60
    ) -> Path:
        calls.append(study)
        if study == "BadStudy":
            raise headless.StudyFatalError("study not found in datasets repo")
        return Path(f"/fake/runs/{study}")

    monkeypatch.setattr(headless, "run_study", fake_run_study)

    results = headless.run(["BadStudy", "GoodStudy"])

    assert calls == ["BadStudy", "GoodStudy"]
    assert results[0].study == "BadStudy"
    assert results[0].run_dir is None
    assert results[0].error is not None
    assert results[1].study == "GoodStudy"
    assert results[1].run_dir == Path("/fake/runs/GoodStudy")
    assert results[1].error is None


# ---------------------------------------------------------------------------
# CLI wiring: `run` subcommand
# ---------------------------------------------------------------------------


def test_cli_run_dispatches_to_headless_run_with_parsed_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        studies: list[str], *, commit: str | None = None, force: bool = False, max_turns: int = 60
    ) -> list[headless.StudyResult]:
        captured["studies"] = studies
        captured["commit"] = commit
        captured["force"] = force
        captured["max_turns"] = max_turns
        return [headless.StudyResult(study=studies[0], run_dir=Path("/fake/run"), error=None)]

    monkeypatch.setattr(cli.headless, "run", fake_run)

    exit_code = cli.main(
        ["run", "MyStudy", "--commit", "abcdef0123", "--force", "--max-turns", "10"]
    )

    assert exit_code == 0
    assert captured == {
        "studies": ["MyStudy"],
        "commit": "abcdef0123",
        "force": True,
        "max_turns": 10,
    }


def test_cli_run_max_turns_defaults_to_60(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(studies: list[str], **kwargs: object) -> list[headless.StudyResult]:
        captured.update(kwargs)
        return [headless.StudyResult(study=studies[0], run_dir=Path("/fake"), error=None)]

    monkeypatch.setattr(cli.headless, "run", fake_run)
    cli.main(["run", "MyStudy"])
    assert captured["max_turns"] == 60
    assert captured["force"] is False
    assert captured["commit"] is None


def test_cli_run_studies_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    studies_file = tmp_path / "studies.txt"
    studies_file.write_text("Alpha\nBeta\n# a comment line\n\nGamma\n")

    captured: dict[str, object] = {}

    def fake_run(studies: list[str], **kwargs: object) -> list[headless.StudyResult]:
        captured["studies"] = studies
        return [
            headless.StudyResult(study=s, run_dir=Path(f"/fake/{s}"), error=None) for s in studies
        ]

    monkeypatch.setattr(cli.headless, "run", fake_run)

    exit_code = cli.main(["run", "--studies-from", str(studies_file)])

    assert exit_code == 0
    assert captured["studies"] == ["Alpha", "Beta", "Gamma"]


def test_cli_run_returns_nonzero_when_any_study_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(studies: list[str], **kwargs: object) -> list[headless.StudyResult]:
        return [headless.StudyResult(study=studies[0], run_dir=None, error="boom")]

    monkeypatch.setattr(cli.headless, "run", fake_run)
    exit_code = cli.main(["run", "Whatever"])
    assert exit_code == 1
