"""Tests for coggym_check.dataset — parsing a study into a StudySnapshot.

Why fixture-driven against a real-shaped mini study: `dataset.py` is the
single deterministic parser every later stage (lint, comparator) trusts, so
these tests exercise the exact file shapes found in the real datasets repo
(``tests/fixtures/studies/TestStudy2020Mini`` is a truncated,
self-consistent copy of Baker2017Rational/exp1's structure, plus an
``exp2`` with no ``block_randomization`` key, mirroring
Gerstenberg2015How). Both filesystem mode (``FsReader``) and commit mode
(``GitReader`` against a throwaway git repo) are tested against the same
fixture data so the two `StudyReader` implementations stay behaviorally
identical.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coggym_check import cli, config, dataset, schemas

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _fixture_datasets_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point config.datasets_repo() at tests/fixtures for every test in this
    module, so dataset.py's filesystem mode reads the fixture study instead
    of the real ../cog-gym-datasets clone.
    """
    monkeypatch.setattr(config, "datasets_repo", lambda: FIXTURES)


# ---------------------------------------------------------------------------
# StudyReader implementations
# ---------------------------------------------------------------------------


class TestFsReader:
    def test_exists_true_for_present_file(self) -> None:
        reader = dataset.FsReader(FIXTURES)
        assert reader.exists("studies/TestStudy2020Mini/exp1/config.json")

    def test_exists_false_for_absent_file(self) -> None:
        reader = dataset.FsReader(FIXTURES)
        assert not reader.exists("studies/TestStudy2020Mini/exp1/paper.pdf")

    def test_read_text_returns_contents(self) -> None:
        reader = dataset.FsReader(FIXTURES)
        text = reader.read_text("studies/TestStudy2020Mini/README.md")
        assert "TestStudy2020Mini" in text

    def test_read_text_missing_file_raises_file_not_found(self) -> None:
        reader = dataset.FsReader(FIXTURES)
        with pytest.raises(FileNotFoundError):
            reader.read_text("studies/TestStudy2020Mini/exp1/nope.json")

    def test_list_entries_distinguishes_dirs_and_files(self) -> None:
        reader = dataset.FsReader(FIXTURES)
        entries = reader.list_entries("studies/TestStudy2020Mini")
        names = {e.name: e.is_dir for e in entries}
        assert names["exp1"] is True
        assert names["exp2"] is True
        assert names["README.md"] is False


class TestGitReader:
    """GitReader against a throwaway repo built from the fixture in tmp_path
    (constraints.md hard rule #3: never checkout — read via `git show`/
    `git ls-tree` only).
    """

    @pytest.fixture()
    def repo(self, tmp_path: Path) -> tuple[Path, str]:
        repo_root = tmp_path / "fake_datasets_repo"
        repo_root.mkdir()
        _copy_tree(FIXTURES / "studies", repo_root / "studies")
        (repo_root / "papers_represented.csv").write_text(
            (FIXTURES / "papers_represented.csv").read_text()
        )
        _git(repo_root, "init", "-q")
        _git(repo_root, "config", "user.email", "test@example.org")
        _git(repo_root, "config", "user.name", "Test")
        _git(repo_root, "add", "-A")
        _git(repo_root, "commit", "-q", "-m", "fixture commit")
        sha = _git(repo_root, "rev-parse", "HEAD").strip()
        # Mirror the real clone's detached HEAD (constraints.md).
        _git(repo_root, "checkout", "-q", "--detach", sha)
        return repo_root, sha

    def test_exists_true_for_present_path(self, repo: tuple[Path, str]) -> None:
        repo_root, sha = repo
        reader = dataset.GitReader(repo_root, sha)
        assert reader.exists("studies/TestStudy2020Mini/exp1/config.json")

    def test_exists_false_for_absent_path(self, repo: tuple[Path, str]) -> None:
        repo_root, sha = repo
        reader = dataset.GitReader(repo_root, sha)
        assert not reader.exists("studies/TestStudy2020Mini/exp1/paper.pdf")

    def test_read_text_matches_working_tree(self, repo: tuple[Path, str]) -> None:
        repo_root, sha = repo
        reader = dataset.GitReader(repo_root, sha)
        got = reader.read_text("studies/TestStudy2020Mini/exp1/config.json")
        expected = (FIXTURES / "studies/TestStudy2020Mini/exp1/config.json").read_text()
        assert got == expected

    def test_read_text_missing_raises_file_not_found(self, repo: tuple[Path, str]) -> None:
        repo_root, sha = repo
        reader = dataset.GitReader(repo_root, sha)
        with pytest.raises(FileNotFoundError):
            reader.read_text("studies/TestStudy2020Mini/exp1/nope.json")

    def test_list_entries_matches_fs(self, repo: tuple[Path, str]) -> None:
        repo_root, sha = repo
        reader = dataset.GitReader(repo_root, sha)
        entries = reader.list_entries("studies/TestStudy2020Mini")
        names = {e.name: e.is_dir for e in entries}
        assert names["exp1"] is True
        assert names["exp2"] is True
        assert names["README.md"] is False

    def test_never_checks_out_or_dirties_repo(self, repo: tuple[Path, str]) -> None:
        """Reading via GitReader must not touch the working tree/index/HEAD."""
        repo_root, sha = repo
        before_status = _git(repo_root, "status", "--porcelain")
        before_head = _git(repo_root, "rev-parse", "HEAD").strip()
        reader = dataset.GitReader(repo_root, sha)
        reader.read_text("studies/TestStudy2020Mini/exp1/config.json")
        reader.list_entries("studies/TestStudy2020Mini/exp1")
        assert _git(repo_root, "status", "--porcelain") == before_status
        assert _git(repo_root, "rev-parse", "HEAD").strip() == before_head


def _copy_tree(src: Path, dst: Path) -> None:
    import shutil

    shutil.copytree(src, dst)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


# ---------------------------------------------------------------------------
# load_study (filesystem mode)
# ---------------------------------------------------------------------------


def test_missing_study_raises_clean_error() -> None:
    with pytest.raises(dataset.StudyNotFoundError):
        dataset.load_study("NoSuchStudy2099")


def test_load_study_returns_study_snapshot() -> None:
    snapshot = dataset.load_study("TestStudy2020Mini")
    assert isinstance(snapshot, schemas.StudySnapshot)
    assert snapshot.study == "TestStudy2020Mini"
    assert snapshot.commit is None
    assert snapshot.paper_pdf_present is False
    assert set(snapshot.experiments) == {"exp1", "exp2"}


def test_source_url_csv_matched_by_study_folder() -> None:
    snapshot = dataset.load_study("TestStudy2020Mini")
    assert snapshot.source_url_csv == "https://example.org/paper"


def test_source_url_csv_none_when_study_absent_from_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A study that genuinely never appears in any row's `experiments`
    column must yield `source_url_csv is None` (the unmatched branch of
    `_lookup_source_url`), even though the CSV file itself is present and
    non-empty.
    """
    repo_root = tmp_path / "no_csv_match_repo"
    repo_root.mkdir()
    _copy_tree(FIXTURES / "studies", repo_root / "studies")
    (repo_root / "papers_represented.csv").write_text(
        "paper_title,authors,Potential reviewer,experiment_count,experiments,source_url\n"
        "Some Other Study,Other Author,,1,OtherStudy2019Foo/exp1,https://example.org/other\n"
    )
    monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

    snapshot = dataset.load_study("TestStudy2020Mini")

    assert snapshot.source_url_csv is None


def test_citation_populated_from_first_experiment_config() -> None:
    snapshot = dataset.load_study("TestStudy2020Mini")
    assert snapshot.citation.paper_title == "A Test Study"
    assert snapshot.citation.paperDOI == "10.1000/test"
    assert snapshot.citation.authors == ["Author, A."]
    assert snapshot.citation.year == 2020


class TestExperimentSnapshotExp1:
    """exp1: 3 trials, 2 instructions, 1 condition WITH block_randomization."""

    @pytest.fixture()
    def exp1(self) -> schemas.ExperimentSnapshot:
        return dataset.load_study("TestStudy2020Mini").experiments["exp1"]

    def test_basic_counts(self, exp1: schemas.ExperimentSnapshot) -> None:
        assert exp1.n_trials_in_jsonl == 3
        assert exp1.n_instructions == 2
        assert exp1.stimuli_count == 3
        assert exp1.task_type == ["Theory of Mind"]
        assert exp1.response_type == ["multi-slider"]

    def test_condition_structure(self, exp1: schemas.ExperimentSnapshot) -> None:
        assert len(exp1.conditions) == 1
        cond = exp1.conditions[0]
        assert cond.name == "cond1"
        assert cond.n_blocks == 2
        assert cond.block_sizes == [2, 3]
        assert cond.block_randomization == [False, False]
        assert cond.block_kinds == ["instruction", "trial"]

    def test_queries_by_tag(self, exp1: schemas.ExperimentSnapshot) -> None:
        assert set(exp1.queries_by_tag) == {"truck_preference", "belief_at_hidden"}
        truck = exp1.queries_by_tag["truck_preference"]
        assert truck.type == "multi-slider"
        assert truck.options == ["Korean", "Mexican"]
        assert truck.n_trials_using == 3
        assert truck.slider is not None
        assert truck.slider.min == 1
        assert truck.slider.max == 7
        assert [label.label for label in truck.slider.labels] == ["not at all", "very much"]

    def test_instruction_texts(self, exp1: schemas.ExperimentSnapshot) -> None:
        assert set(exp1.instruction_texts) == {"instruction_intro", "instruction_task"}
        assert "Welcome" in exp1.instruction_texts["instruction_intro"]

    def test_stimuli_modalities(self, exp1: schemas.ExperimentSnapshot) -> None:
        assert exp1.stimuli_modalities == ["video"]

    def test_assets_and_extra_files(self, exp1: schemas.ExperimentSnapshot) -> None:
        assert exp1.assets_present is False
        assert exp1.extra_files == []

    def test_human_data(self, exp1: schemas.ExperimentSnapshot) -> None:
        assert exp1.human_data.participants_count == 5
        assert exp1.human_data.judgment_count == 15


class TestExperimentSnapshotExp2:
    """exp2: same shape, but NO block_randomization key at all."""

    @pytest.fixture()
    def exp2(self) -> schemas.ExperimentSnapshot:
        return dataset.load_study("TestStudy2020Mini").experiments["exp2"]

    def test_basic_counts(self, exp2: schemas.ExperimentSnapshot) -> None:
        assert exp2.n_trials_in_jsonl == 3
        assert exp2.n_instructions == 2
        assert exp2.stimuli_count == 3

    def test_block_randomization_absent_is_none(self, exp2: schemas.ExperimentSnapshot) -> None:
        cond = exp2.conditions[0]
        assert cond.block_randomization is None
        assert cond.block_sizes == [2, 3]
        assert cond.block_kinds == ["instruction", "trial"]

    def test_single_slider_query(self, exp2: schemas.ExperimentSnapshot) -> None:
        query = exp2.queries_by_tag["causal_rating"]
        assert query.type == "single-slider"
        assert query.n_trials_using == 3
        assert query.slider is not None
        assert query.slider.min == 0
        assert query.slider.max == 100


def test_block_kinds_mixed_and_unresolvable_ids_do_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block whose ids don't cleanly resolve to either instruction.jsonl or
    trial.jsonl must classify as "mixed" rather than crashing the snapshot —
    both for a block that straddles an instruction id and a trial id, and
    for a block containing an id that resolves nowhere at all (unresolvable
    ids are lint's job to flag, not dataset.py's job to raise on).
    """
    repo_root = tmp_path / "mixed_blocks_repo"
    repo_root.mkdir()
    _copy_tree(FIXTURES / "studies", repo_root / "studies")
    config_path = repo_root / "studies/TestStudy2020Mini/exp1/config.json"
    config_data = json.loads(config_path.read_text())
    config_data["experimentFlow"].append(
        {
            "experimental_condition": "cond_mixed",
            "blocks": [
                ["instruction_intro", "scenario_001"],
                ["totally_unresolvable_id"],
            ],
        }
    )
    config_path.write_text(json.dumps(config_data))
    monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

    snapshot = dataset.load_study("TestStudy2020Mini")

    cond = snapshot.experiments["exp1"].conditions[1]
    assert cond.name == "cond_mixed"
    assert cond.n_blocks == 2
    assert cond.block_sizes == [2, 1]
    assert cond.block_kinds == ["mixed", "mixed"]


def test_human_data_missing_file_yields_none_participants_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An experiment whose `human_data_mean.json` is entirely absent must
    still produce a snapshot, with `human_data.participants_count` (and
    `judgment_count`) coming back `None` rather than raising.
    """
    repo_root = tmp_path / "missing_human_data_repo"
    repo_root.mkdir()
    _copy_tree(FIXTURES / "studies", repo_root / "studies")
    (repo_root / "studies/TestStudy2020Mini/exp1/human_data_mean.json").unlink()
    monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

    snapshot = dataset.load_study("TestStudy2020Mini")

    exp1 = snapshot.experiments["exp1"]
    assert exp1.human_data.participants_count is None
    assert exp1.human_data.judgment_count is None


def test_human_data_malformed_participant_count_yields_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `participants_info.count` that isn't a plain int (e.g. spelled out
    as a string) must yield `participants_count is None` rather than raising
    or silently coercing.
    """
    repo_root = tmp_path / "malformed_human_data_repo"
    repo_root.mkdir()
    _copy_tree(FIXTURES / "studies", repo_root / "studies")
    human_data_path = (
        repo_root / "studies/TestStudy2020Mini/exp1/human_data_mean.json"
    )
    human_data = json.loads(human_data_path.read_text())
    human_data["participants_info"]["count"] = "sixteen"
    human_data_path.write_text(json.dumps(human_data))
    monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

    snapshot = dataset.load_study("TestStudy2020Mini")

    assert snapshot.experiments["exp1"].human_data.participants_count is None


# ---------------------------------------------------------------------------
# Commit mode
# ---------------------------------------------------------------------------


def test_load_study_commit_mode_matches_fs_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "fake_datasets_repo"
    repo_root.mkdir()
    _copy_tree(FIXTURES / "studies", repo_root / "studies")
    (repo_root / "papers_represented.csv").write_text(
        (FIXTURES / "papers_represented.csv").read_text()
    )
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "test@example.org")
    _git(repo_root, "config", "user.name", "Test")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "fixture commit")
    sha = _git(repo_root, "rev-parse", "HEAD").strip()
    _git(repo_root, "checkout", "-q", "--detach", sha)

    monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

    fs_snapshot = dataset.load_study("TestStudy2020Mini")
    commit_snapshot = dataset.load_study("TestStudy2020Mini", commit=sha)

    assert commit_snapshot.commit == sha
    assert commit_snapshot.experiments == fs_snapshot.experiments
    assert commit_snapshot.citation == fs_snapshot.citation
    assert commit_snapshot.source_url_csv == fs_snapshot.source_url_csv


def test_load_study_commit_mode_missing_study_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "fake_datasets_repo"
    repo_root.mkdir()
    _copy_tree(FIXTURES / "studies", repo_root / "studies")
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "test@example.org")
    _git(repo_root, "config", "user.name", "Test")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "fixture commit")
    sha = _git(repo_root, "rev-parse", "HEAD").strip()

    monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)

    with pytest.raises(dataset.StudyNotFoundError):
        dataset.load_study("NoSuchStudy2099", commit=sha)


# ---------------------------------------------------------------------------
# CLI: snapshot subcommand
# ---------------------------------------------------------------------------


def test_cli_snapshot_writes_valid_artifact_and_prints_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["snapshot", "TestStudy2020Mini"])

    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    run_dir = Path(out)
    assert run_dir == tmp_path / "runs" / "TestStudy2020Mini"
    artifact_path = run_dir / "study_snapshot.json"
    assert artifact_path.exists()
    data = json.loads(artifact_path.read_text())
    schemas.StudySnapshot.model_validate(data)
    assert data["study"] == "TestStudy2020Mini"
    assert set(data["experiments"]) == {"exp1", "exp2"}


def test_cli_snapshot_with_commit_uses_shortsha_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = tmp_path / "fake_datasets_repo"
    repo_root.mkdir()
    _copy_tree(FIXTURES / "studies", repo_root / "studies")
    (repo_root / "papers_represented.csv").write_text(
        (FIXTURES / "papers_represented.csv").read_text()
    )
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "test@example.org")
    _git(repo_root, "config", "user.name", "Test")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-q", "-m", "fixture commit")
    sha = _git(repo_root, "rev-parse", "HEAD").strip()
    _git(repo_root, "checkout", "-q", "--detach", sha)

    monkeypatch.setattr(config, "datasets_repo", lambda: repo_root)
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["snapshot", "TestStudy2020Mini", "--commit", sha])

    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    run_dir = Path(out)
    assert run_dir == tmp_path / "runs" / f"TestStudy2020Mini@{sha[:9]}"
    artifact_path = run_dir / "study_snapshot.json"
    data = json.loads(artifact_path.read_text())
    assert data["commit"] == sha


def test_cli_snapshot_missing_study_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    exit_code = cli.main(["snapshot", "NoSuchStudy2099"])
    assert exit_code != 0
