"""Tests for coggym_check.schemas — the pydantic v2 artifact models.

Why fixture-driven: schemas.py is the single source of truth for every run
artifact (constraints.md hard rule #2). Validating real JSON fixtures (not
just constructing Python objects in-line) exercises the same path
`validate-artifact` takes against real stage output, and pins down the exact
field names/types every later pipeline stage depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from coggym_check import cli, schemas

FIXTURES = Path(__file__).parent / "fixtures" / "artifacts"

# (basename, model) pairs mirror the exact filename map the brief specifies
# for `validate-artifact`. Keeping this list in schemas.FILENAME_MODEL_MAP's
# order (rather than re-deriving it) means a mismatch between the two shows
# up as a visible test failure instead of silently drifting apart.
ARTIFACT_CASES = list(schemas.FILENAME_MODEL_MAP.items())


@pytest.mark.parametrize("basename,model", ARTIFACT_CASES, ids=[c[0] for c in ARTIFACT_CASES])
def test_valid_fixture_validates(basename: str, model: type) -> None:
    """Every artifact type has a hand-written valid sample that must pass."""
    data = json.loads((FIXTURES / "valid" / basename).read_text())
    instance = model.model_validate(data)
    assert isinstance(instance, model)


@pytest.mark.parametrize("basename,model", ARTIFACT_CASES, ids=[c[0] for c in ARTIFACT_CASES])
def test_invalid_fixture_rejected(basename: str, model: type) -> None:
    """Every artifact type has a sample with exactly one seeded violation
    (wrong literal / missing required field / fix-without-clear-cut) that
    must be rejected.
    """
    data = json.loads((FIXTURES / "invalid" / basename).read_text())
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_filename_model_map_covers_all_nine_artifacts() -> None:
    """The exact basename->model map the brief lists, verbatim."""
    assert set(schemas.FILENAME_MODEL_MAP) == {
        "study_snapshot.json",
        "lint.json",
        "paper_status.json",
        "materials_manifest.json",
        "paper_summary.json",
        "materials_summary.json",
        "comparison.json",
        "fix_plan.json",
        "run_meta.json",
    }
    assert schemas.FILENAME_MODEL_MAP["study_snapshot.json"] is schemas.StudySnapshot
    assert schemas.FILENAME_MODEL_MAP["lint.json"] is schemas.LintReport
    assert schemas.FILENAME_MODEL_MAP["paper_status.json"] is schemas.PaperStatus
    assert schemas.FILENAME_MODEL_MAP["materials_manifest.json"] is schemas.MaterialsManifest
    assert schemas.FILENAME_MODEL_MAP["paper_summary.json"] is schemas.PaperSummary
    assert schemas.FILENAME_MODEL_MAP["materials_summary.json"] is schemas.MaterialsSummary
    assert schemas.FILENAME_MODEL_MAP["comparison.json"] is schemas.Comparison
    assert schemas.FILENAME_MODEL_MAP["fix_plan.json"] is schemas.FixPlan
    assert schemas.FILENAME_MODEL_MAP["run_meta.json"] is schemas.RunMeta


class TestSourced:
    """The `Sourced` wrapper used across PaperSummary/MaterialsSummary."""

    def test_minimal_sourced(self) -> None:
        s = schemas.Sourced(value=None, quote=None, ref=None, confidence="low", source="inferred")
        assert s.value is None
        assert s.confidence == "low"

    def test_sourced_rejects_bad_confidence_literal(self) -> None:
        with pytest.raises(ValidationError):
            schemas.Sourced(value=1, quote=None, ref=None, confidence="certain", source="paper")

    def test_sourced_rejects_bad_source_literal(self) -> None:
        with pytest.raises(ValidationError):
            schemas.Sourced(value=1, quote=None, ref=None, confidence="high", source="internet")

    def test_sourced_value_accepts_any_type(self) -> None:
        for v in (1, "text", ["a", "b"], {"k": "v"}, True, None):
            s = schemas.Sourced(value=v, quote=None, ref=None, confidence="high", source="paper")
            assert s.value == v


class TestFixOpDiscriminatedUnion:
    """FixOp's four op variants are exhaustive and discriminated on `op`."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"op": "set_config_field", "experiment": "exp1", "key_path": "responseType.0", "new_value": "slider"},
            {"op": "set_instruction_text", "experiment": "exp1", "record_id": "instr1", "new_value": "New text."},
            {
                "op": "set_block_randomization",
                "experiment": "exp1",
                "condition_index": 0,
                "block_index": 1,
                "new_value": True,
            },
            {
                "op": "set_slider_labels",
                "experiment": "exp1",
                "trial_id": "t1",
                "query_tag": "q1",
                "new_labels": ["low", "high"],
                "new_min": 0.0,
                "new_max": 1.0,
            },
        ],
    )
    def test_each_op_variant_round_trips_inside_discrepancy(self, payload: dict) -> None:
        discrepancy = _base_discrepancy(recommendation="flag", clear_cut=False, proposed_fix=None)
        d = discrepancy.copy()
        d["recommendation"] = "fix"
        d["clear_cut"] = True
        d["proposed_fix"] = payload
        instance = schemas.Discrepancy.model_validate(d)
        assert instance.proposed_fix.op == payload["op"]

    def test_unknown_op_rejected(self) -> None:
        d = _base_discrepancy(recommendation="flag", clear_cut=False, proposed_fix=None)
        d["proposed_fix"] = {"op": "delete_everything", "experiment": "exp1"}
        with pytest.raises(ValidationError):
            schemas.Discrepancy.model_validate(d)


class TestDiscrepancyFixClearCutValidator:
    """recommendation == "fix" requires clear_cut == True and proposed_fix set."""

    def test_flag_without_proposed_fix_is_fine(self) -> None:
        d = _base_discrepancy(recommendation="flag", clear_cut=False, proposed_fix=None)
        schemas.Discrepancy.model_validate(d)

    def test_fix_with_clear_cut_and_proposed_fix_is_fine(self) -> None:
        d = _base_discrepancy(
            recommendation="fix",
            clear_cut=True,
            proposed_fix={
                "op": "set_instruction_text",
                "experiment": "exp1",
                "record_id": "instr1",
                "new_value": "New text.",
            },
        )
        schemas.Discrepancy.model_validate(d)

    def test_fix_without_clear_cut_rejected(self) -> None:
        d = _base_discrepancy(
            recommendation="fix",
            clear_cut=False,
            proposed_fix={
                "op": "set_instruction_text",
                "experiment": "exp1",
                "record_id": "instr1",
                "new_value": "New text.",
            },
        )
        with pytest.raises(ValidationError):
            schemas.Discrepancy.model_validate(d)

    def test_fix_without_proposed_fix_rejected(self) -> None:
        d = _base_discrepancy(recommendation="fix", clear_cut=True, proposed_fix=None)
        with pytest.raises(ValidationError):
            schemas.Discrepancy.model_validate(d)


def _base_discrepancy(*, recommendation: str, clear_cut: bool, proposed_fix: dict | None) -> dict:
    return {
        "id": "d1",
        "category": "instruction_text",
        "field": "instruction_texts.instr1",
        "location": {"file": "instruction.jsonl", "record_id": "instr1", "json_path": None},
        "paper_value": "A",
        "materials_value": "B",
        "coggym_value": "B",
        "severity": "minor",
        "clear_cut": clear_cut,
        "evidence": [],
        "recommendation": recommendation,
        "rationale": "test",
        "proposed_fix": proposed_fix,
    }


class TestExtraFieldsForbidden:
    """Artifact models reject unknown fields so producer typos surface at
    validation time rather than being silently dropped or ignored.
    """

    def test_study_snapshot_rejects_unknown_top_level_field(self) -> None:
        data = json.loads((FIXTURES / "valid" / "study_snapshot.json").read_text())
        data["not_a_real_field"] = True
        with pytest.raises(ValidationError):
            schemas.StudySnapshot.model_validate(data)


class TestExportSchemasMarkdown:
    """render_schemas_markdown() feeds the `export-schemas` CLI subcommand."""

    def test_contains_every_model_and_filename(self) -> None:
        md = schemas.render_schemas_markdown()
        for basename, model in schemas.FILENAME_MODEL_MAP.items():
            assert basename in md
            assert model.__name__ in md
            assert schemas.ARTIFACT_PURPOSES[basename] in md

    def test_looks_like_valid_markdown_with_json_fences(self) -> None:
        md = schemas.render_schemas_markdown()
        assert md.count("```json") == md.count("```") / 2
        assert md.startswith("# ")


class TestValidateArtifactCli:
    """CLI-level: `validate-artifact` exit codes and unknown-filename error."""

    @pytest.mark.parametrize("basename", [c[0] for c in ARTIFACT_CASES], ids=[c[0] for c in ARTIFACT_CASES])
    def test_valid_artifact_exits_zero(self, basename: str) -> None:
        path = FIXTURES / "valid" / basename
        assert cli.main(["validate-artifact", str(path)]) == 0

    @pytest.mark.parametrize("basename", [c[0] for c in ARTIFACT_CASES], ids=[c[0] for c in ARTIFACT_CASES])
    def test_invalid_artifact_exits_one(self, basename: str, capsys: pytest.CaptureFixture) -> None:
        path = FIXTURES / "invalid" / basename
        code = cli.main(["validate-artifact", str(path)])
        assert code == 1
        captured = capsys.readouterr()
        assert captured.err.strip() != ""

    def test_unknown_basename_exits_two_and_lists_known_names(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        bogus = tmp_path / "some_unknown_artifact.json"
        bogus.write_text("{}")
        code = cli.main(["validate-artifact", str(bogus)])
        assert code == 2
        err = capsys.readouterr().err
        assert "some_unknown_artifact.json" in err
        for basename in schemas.FILENAME_MODEL_MAP:
            assert basename in err

    def test_malformed_json_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        bad = tmp_path / "lint.json"
        bad.write_text("{not valid json")
        code = cli.main(["validate-artifact", str(bad)])
        assert code == 1
        assert capsys.readouterr().err.strip() != ""


class TestExportSchemasCli:
    def test_writes_default_docs_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Redirect repo_root() so the CLI's default --out target lands in
        # tmp_path instead of writing over the real docs/artifacts.md.
        monkeypatch.setattr(cli.config, "repo_root", lambda: tmp_path)
        code = cli.main(["export-schemas"])
        assert code == 0
        out = tmp_path / "docs" / "artifacts.md"
        assert out.exists()
        assert "StudySnapshot" in out.read_text()

    def test_writes_custom_out_path(self, tmp_path: Path) -> None:
        out = tmp_path / "custom" / "schemas.md"
        code = cli.main(["export-schemas", "--out", str(out)])
        assert code == 0
        assert out.exists()
        assert "RunMeta" in out.read_text()
