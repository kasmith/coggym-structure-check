"""Tests for coggym_check.lint — deterministic structural lint (no LLM).

Two fixture roots are used:

- ``tests/fixtures`` (the shared Task 3 ``TestStudy2020Mini``) is the "clean"
  baseline for every check except ``assets-exist``: it produces zero
  findings for every check_id.
- ``tests/fixtures/studies_broken`` holds one tiny variant study per seeded
  violation named in the task brief (dangling block id, randomization
  length mismatch, missing asset, slider min>max, orphan trial id, missing
  human-data file, human-data key not in trials, malformed JSON), each
  derived from the same minimal single-trial/single-instruction shape.

``assets-exist`` is the one check the shared Mini fixture cannot cleanly
exercise: Mini's trials reference ``assets/foodtruck*.mp4``, but Mini has no
``assets/`` directory on disk (Task 3 deliberately built it without real
media so ``assets_present`` is `False` and `test_dataset.py` asserts that).
So this check's "clean" case is instead built by copying the
``AssetsMissing2020Bad`` broken fixture into a tmp dir and adding back the
one file it's missing — same pattern `test_dataset.py` uses for its own
tmp_path-based variations.

Checks with no dedicated ``studies_broken`` fixture (``stimuli-count``,
``response-type``, ``citation-present``, ``doi-shape``, ``extra-files``,
``non-experiment-dirs``) get their broken case the same way: copy Mini into
a tmp dir and apply one small, targeted mutation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coggym_check import cli, config, dataset, lint, schemas

FIXTURES = Path(__file__).parent / "fixtures"
BROKEN_ROOT = FIXTURES / "studies_broken"


@pytest.fixture()
def clean_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point config.datasets_repo() at the shared Mini fixture's root."""
    monkeypatch.setattr(config, "datasets_repo", lambda: FIXTURES)


@pytest.fixture()
def broken_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point config.datasets_repo() at the seeded-violation fixtures' root."""
    monkeypatch.setattr(config, "datasets_repo", lambda: BROKEN_ROOT)


def _findings(report: schemas.LintReport, check_id: str) -> list[schemas.LintFinding]:
    return [f for f in report.findings if f.check_id == check_id]


def _copy_mini(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    shutil.copytree(FIXTURES / "studies", repo_root / "studies")
    (repo_root / "papers_represented.csv").write_text(
        (FIXTURES / "papers_represented.csv").read_text()
    )
    return repo_root


def _copy_broken(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    shutil.copytree(BROKEN_ROOT / "studies", repo_root / "studies")
    return repo_root


def _mutate_json(path: Path, mutate) -> None:
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def _make_detached_git_repo(repo_root: Path) -> str:
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "test@example.org")
    _git(repo_root, "config", "user.name", "Test")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "fixture commit")
    sha = _git(repo_root, "rev-parse", "HEAD").strip()
    _git(repo_root, "checkout", "-q", "--detach", sha)
    return sha


# ---------------------------------------------------------------------------
# lint_study: study-not-found
# ---------------------------------------------------------------------------


def test_lint_study_missing_study_raises(clean_repo: None) -> None:
    with pytest.raises(dataset.StudyNotFoundError):
        lint.lint_study("NoSuchStudy2099")


def test_checks_run_lists_every_check_id(clean_repo: None) -> None:
    report = lint.lint_study("TestStudy2020Mini")
    assert set(report.checks_run) == set(lint.ALL_CHECK_IDS)


# ---------------------------------------------------------------------------
# files-present
# ---------------------------------------------------------------------------


class TestFilesPresent:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "files-present") == []

    def test_missing_human_data_file_errors(self, broken_repo: None) -> None:
        report = lint.lint_study("MissingHumanData2020Bad")
        findings = _findings(report, "files-present")
        assert len(findings) == 1
        assert findings[0].level == "error"
        assert "human_data_mean.json" in findings[0].message


# ---------------------------------------------------------------------------
# json-parse
# ---------------------------------------------------------------------------


class TestJsonParse:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "json-parse") == []

    def test_malformed_config_json_errors(self, broken_repo: None) -> None:
        report = lint.lint_study("MalformedJson2020Bad")
        findings = _findings(report, "json-parse")
        assert len(findings) == 1
        assert findings[0].level == "error"
        assert "config.json" in findings[0].message

    def test_malformed_experiment_skips_config_dependent_checks_but_not_others(
        self, broken_repo: None
    ) -> None:
        """A study-breaking config.json must not abort the whole study: other
        experiments (none here) would still be linted, and checks that don't
        need config.json (files-present, slider-sane, assets-exist,
        human-data-ids) still run for this experiment."""
        report = lint.lint_study("MalformedJson2020Bad")
        assert _findings(report, "files-present") == []
        assert _findings(report, "flow-ids-resolve") == []
        assert _findings(report, "randomization-length") == []
        assert _findings(report, "citation-present") == []
        assert _findings(report, "response-type") == []


# ---------------------------------------------------------------------------
# flow-ids-resolve
# ---------------------------------------------------------------------------


class TestFlowIdsResolve:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "flow-ids-resolve") == []

    def test_dangling_block_id_errors(self, broken_repo: None) -> None:
        report = lint.lint_study("DanglingBlockId2020Bad")
        findings = _findings(report, "flow-ids-resolve")
        assert len(findings) == 1
        assert findings[0].level == "error"
        assert "nonexistent_id" in findings[0].message


# ---------------------------------------------------------------------------
# ids-referenced
# ---------------------------------------------------------------------------


class TestIdsReferenced:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "ids-referenced") == []

    def test_orphan_trial_id_warns(self, broken_repo: None) -> None:
        report = lint.lint_study("OrphanTrialId2020Bad")
        findings = _findings(report, "ids-referenced")
        assert len(findings) == 1
        assert findings[0].level == "warning"
        assert "trial_2" in findings[0].message


# ---------------------------------------------------------------------------
# randomization-length
# ---------------------------------------------------------------------------


class TestRandomizationLength:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "randomization-length") == []

    def test_exp2_no_block_randomization_key_has_no_findings(self, clean_repo: None) -> None:
        """exp2 omits block_randomization entirely (Gerstenberg2015How-style)
        -- absence must not itself be a finding."""
        report = lint.lint_study("TestStudy2020Mini")
        assert all(
            f.experiment != "exp2" for f in _findings(report, "randomization-length")
        )

    def test_length_mismatch_errors(self, broken_repo: None) -> None:
        report = lint.lint_study("RandomizationMismatch2020Bad")
        findings = _findings(report, "randomization-length")
        assert len(findings) == 1
        assert findings[0].level == "error"


# ---------------------------------------------------------------------------
# stimuli-count
# ---------------------------------------------------------------------------


class TestStimuliCount:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "stimuli-count") == []

    def test_mismatch_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_root = _copy_mini(tmp_path)
        _mutate_json(
            repo_root / "studies/TestStudy2020Mini/exp1/config.json",
            lambda c: c.__setitem__("stimuli_count", 999),
        )
        monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

        report = lint.lint_study("TestStudy2020Mini")

        findings = _findings(report, "stimuli-count")
        assert len(findings) == 1
        assert findings[0].level == "warning"
        assert findings[0].experiment == "exp1"


# ---------------------------------------------------------------------------
# response-type
# ---------------------------------------------------------------------------


class TestResponseType:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "response-type") == []

    def test_mismatch_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_root = _copy_mini(tmp_path)
        _mutate_json(
            repo_root / "studies/TestStudy2020Mini/exp1/config.json",
            lambda c: c.__setitem__("responseType", ["multi-choice"]),
        )
        monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

        report = lint.lint_study("TestStudy2020Mini")

        findings = _findings(report, "response-type")
        assert len(findings) == 1
        assert findings[0].level == "warning"
        assert findings[0].experiment == "exp1"


# ---------------------------------------------------------------------------
# assets-exist
# ---------------------------------------------------------------------------


class TestAssetsExist:
    def test_when_asset_present_no_findings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_root = _copy_broken(tmp_path)
        assets_dir = repo_root / "studies/AssetsMissing2020Bad/exp1/assets"
        assets_dir.mkdir(parents=True)
        (assets_dir / "img1.png").write_bytes(b"not-a-real-png")
        monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

        report = lint.lint_study("AssetsMissing2020Bad")

        assert _findings(report, "assets-exist") == []

    def test_missing_asset_errors(self, broken_repo: None) -> None:
        report = lint.lint_study("AssetsMissing2020Bad")
        findings = _findings(report, "assets-exist")
        assert len(findings) == 1
        assert findings[0].level == "error"
        assert "img1.png" in findings[0].message

    def test_commit_mode_emits_info_only_and_skips_fs_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_root = _copy_broken(tmp_path)
        sha = _make_detached_git_repo(repo_root)
        monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

        report = lint.lint_study("AssetsMissing2020Bad", commit=sha)

        findings = _findings(report, "assets-exist")
        assert len(findings) == 1
        assert findings[0].level == "info"
        assert "commit mode" in findings[0].message


# ---------------------------------------------------------------------------
# slider-sane
# ---------------------------------------------------------------------------


class TestSliderSane:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "slider-sane") == []

    def test_min_greater_than_max_errors(self, broken_repo: None) -> None:
        report = lint.lint_study("SliderMinMax2020Bad")
        findings = _findings(report, "slider-sane")
        assert len(findings) == 1
        assert findings[0].level == "error"
        assert "min" in findings[0].message and "max" in findings[0].message


# ---------------------------------------------------------------------------
# human-data-ids
# ---------------------------------------------------------------------------


class TestHumanDataIds:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "human-data-ids") == []

    def test_orphan_key_warns(self, broken_repo: None) -> None:
        report = lint.lint_study("HumanDataKeyMismatch2020Bad")
        findings = _findings(report, "human-data-ids")
        assert len(findings) == 1
        assert findings[0].level == "warning"
        assert "trial_ghost" in findings[0].message


# ---------------------------------------------------------------------------
# citation-present
# ---------------------------------------------------------------------------


class TestCitationPresent:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "citation-present") == []

    def test_missing_authors_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_root = _copy_mini(tmp_path)
        _mutate_json(
            repo_root / "studies/TestStudy2020Mini/exp1/config.json",
            lambda c: c.pop("authors", None),
        )
        monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

        report = lint.lint_study("TestStudy2020Mini")

        findings = _findings(report, "citation-present")
        assert len(findings) == 1
        assert findings[0].level == "warning"
        assert "authors" in findings[0].message


# ---------------------------------------------------------------------------
# doi-shape
# ---------------------------------------------------------------------------


class TestDoiShape:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        """Mini's paperDOI is the bare-DOI-shaped '10.1000/test' -- a valid
        shape, so this must produce no finding."""
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "doi-shape") == []

    def test_empty_doi_is_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_root = _copy_mini(tmp_path)
        _mutate_json(
            repo_root / "studies/TestStudy2020Mini/exp1/config.json",
            lambda c: c.__setitem__("paperDOI", ""),
        )
        monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

        report = lint.lint_study("TestStudy2020Mini")

        findings = _findings(report, "doi-shape")
        assert len(findings) == 1
        assert findings[0].level == "info"

    def test_non_doi_url_is_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_root = _copy_mini(tmp_path)
        _mutate_json(
            repo_root / "studies/TestStudy2020Mini/exp1/config.json",
            lambda c: c.__setitem__("paperDOI", "https://example.org/papers/test.pdf"),
        )
        monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

        report = lint.lint_study("TestStudy2020Mini")

        findings = _findings(report, "doi-shape")
        assert len(findings) == 1
        assert findings[0].level == "info"

    def test_doi_org_url_shape_is_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_root = _copy_mini(tmp_path)
        _mutate_json(
            repo_root / "studies/TestStudy2020Mini/exp1/config.json",
            lambda c: c.__setitem__("paperDOI", "https://doi.org/10.1000/test"),
        )
        monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

        report = lint.lint_study("TestStudy2020Mini")

        assert _findings(report, "doi-shape") == []


# ---------------------------------------------------------------------------
# extra-files
# ---------------------------------------------------------------------------


class TestExtraFiles:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "extra-files") == []

    def test_stray_file_is_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_root = _copy_mini(tmp_path)
        (repo_root / "studies/TestStudy2020Mini/exp1/scratch_notes.txt").write_text("x")
        monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

        report = lint.lint_study("TestStudy2020Mini")

        findings = _findings(report, "extra-files")
        assert len(findings) == 1
        assert findings[0].level == "info"
        assert "scratch_notes.txt" in findings[0].message


# ---------------------------------------------------------------------------
# non-experiment-dirs
# ---------------------------------------------------------------------------


class TestNonExperimentDirs:
    def test_clean_mini_has_no_findings(self, clean_repo: None) -> None:
        report = lint.lint_study("TestStudy2020Mini")
        assert _findings(report, "non-experiment-dirs") == []

    def test_bare_dir_without_config_json_is_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors the real Baker2017Rational/exp2 case: a bare directory
        with no config.json (here holding just a stray subfolder) must be
        skipped as non-experiment and surfaced as an info finding, not
        crash the lint."""
        repo_root = _copy_mini(tmp_path)
        bare_dir = repo_root / "studies/TestStudy2020Mini/exp3/symbolic_representation"
        bare_dir.mkdir(parents=True)
        monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

        report = lint.lint_study("TestStudy2020Mini")

        findings = _findings(report, "non-experiment-dirs")
        assert len(findings) == 1
        assert findings[0].level == "info"
        assert "exp3" in findings[0].message


# ---------------------------------------------------------------------------
# CLI: lint subcommand
# ---------------------------------------------------------------------------


def test_cli_lint_writes_valid_artifact_and_exits_zero_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exercises a fully clean study end to end. Mini's own on-disk copy
    (tests/fixtures/studies/TestStudy2020Mini) references media_url paths
    under assets/ that don't exist on disk (Task 3 built it without real
    media, since dataset.py's own tests only assert assets_present is
    False) -- so a real assets-exist error is expected there and covered by
    TestAssetsExist above. Here, to test the "everything genuinely clean"
    CLI path, copy Mini into tmp_path and add back the referenced asset
    files."""
    repo_root = _copy_mini(tmp_path)
    for exp, names in (
        ("exp1", ["foodtruck1.mp4", "foodtruck2.mp4", "foodtruck3.mp4"]),
        ("exp2", ["clip1.mp4", "clip2.mp4", "clip3.mp4"]),
    ):
        assets_dir = repo_root / "studies/TestStudy2020Mini" / exp / "assets"
        assets_dir.mkdir(parents=True)
        for name in names:
            (assets_dir / name).write_bytes(b"fake-media-bytes")
    monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["lint", "TestStudy2020Mini"])

    assert exit_code == 0
    out = capsys.readouterr().out
    run_dir = tmp_path / "runs" / "TestStudy2020Mini"
    assert str(run_dir) in out
    artifact_path = run_dir / "lint.json"
    assert artifact_path.exists()
    data = json.loads(artifact_path.read_text())
    schemas.LintReport.model_validate(data)
    assert data["study"] == "TestStudy2020Mini"


def test_cli_lint_exits_nonzero_when_error_level_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "datasets_repo", lambda: BROKEN_ROOT)
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["lint", "DanglingBlockId2020Bad"])

    assert exit_code == 1
    data = json.loads((tmp_path / "runs/DanglingBlockId2020Bad/lint.json").read_text())
    schemas.LintReport.model_validate(data)
    assert any(f["level"] == "error" for f in data["findings"])


def test_cli_lint_exits_nonzero_only_for_errors_not_warnings_or_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OrphanTrialId2020Bad's only finding is a warning (ids-referenced) --
    must exit 0."""
    monkeypatch.setattr(config, "datasets_repo", lambda: BROKEN_ROOT)
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["lint", "OrphanTrialId2020Bad"])

    assert exit_code == 0


def test_cli_lint_missing_study_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "datasets_repo", lambda: FIXTURES)
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["lint", "NoSuchStudy2099"])

    assert exit_code != 0
