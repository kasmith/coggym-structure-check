"""Tests for coggym_check.paper — paper.pdf discovery, text extraction, and
the `extract-paper` CLI subcommand.

Mock-free per constraints.md hard rule #8: rather than committing a binary
PDF fixture, every test builds its own tiny PDF at test time with pymupdf
itself (`_write_fixture_pdf` below) — real page objects, real
`get_text()` extraction, no network, no LLM.

Fixture shape (matches the task brief's "two text pages, one near-empty
page"): page 1 and page 2 each carry >=200 extractable characters; page 3 is
left blank (0 extractable characters). That gives a deterministic
`text_quality` of exactly 2/3 to assert against.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

from coggym_check import cli, config, paper, schemas

STUDY = "TestStudy2020Mini"

#: A single line long enough that four repeats clear the 200-char floor
#: (matches the brief's "≥200 extractable characters" threshold exactly).
_LINE = "The quick brown fox jumps over the lazy dog 0123456789. "


def _write_fixture_pdf(path: Path) -> None:
    """Write a 3-page PDF to `path`: two text-bearing pages, one near-empty."""
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page(width=612, height=792)
        for i in range(4):
            page.insert_text((50, 50 + i * 20), _LINE, fontsize=11)
    doc.new_page(width=612, height=792)  # page 3: no text inserted at all
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


@pytest.fixture()
def repo_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point config.datasets_repo()/materials_dir() at isolated tmp dirs."""
    datasets_repo = tmp_path / "cog-gym-datasets"
    materials_dir = tmp_path / "materials"
    monkeypatch.setattr(config, "datasets_repo", lambda: datasets_repo)
    monkeypatch.setattr(config, "materials_dir", lambda: materials_dir)
    return datasets_repo, materials_dir


# ---------------------------------------------------------------------------
# found_local: studies/<study>/paper.pdf present
# ---------------------------------------------------------------------------


def test_extract_paper_found_local_writes_pages_and_quality(
    repo_paths: tuple[Path, Path],
) -> None:
    datasets_repo, materials_dir = repo_paths
    _write_fixture_pdf(datasets_repo / "studies" / STUDY / "paper.pdf")

    status = paper.extract_paper(STUDY)

    assert status.study == STUDY
    assert status.status == "found_local"
    assert status.n_pages == 3
    assert status.text_quality is not None
    assert status.text_quality == pytest.approx(2 / 3)
    # 2/3 >= 0.5, so no direct-read fallback is needed.
    assert status.needs_direct_pdf_read is False
    assert status.pdf_path == f"materials/{STUDY}/paper.pdf"
    assert status.retrieved_from == f"studies/{STUDY}/paper.pdf"
    assert status.candidates_tried == []

    text_dir = materials_dir / STUDY / "paper_text"
    page_files = sorted(text_dir.iterdir())
    assert [p.name for p in page_files] == ["page_001.txt", "page_002.txt", "page_003.txt"]
    assert len(page_files[0].read_text()) >= 200
    assert len(page_files[1].read_text()) >= 200
    assert len(page_files[2].read_text()) < 200

    dest_pdf = materials_dir / STUDY / "paper.pdf"
    assert dest_pdf.exists()
    assert status.sha256 == hashlib.sha256(dest_pdf.read_bytes()).hexdigest()


def test_extract_paper_writes_validated_paper_status_json_via_cli(
    repo_paths: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasets_repo, _materials_dir = repo_paths
    _write_fixture_pdf(datasets_repo / "studies" / STUDY / "paper.pdf")
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["extract-paper", STUDY])

    assert exit_code == 0
    run_dir = tmp_path / "runs" / STUDY
    artifact_path = run_dir / "paper_status.json"
    assert artifact_path.exists()
    schemas.PaperStatus.model_validate_json(artifact_path.read_text())


# ---------------------------------------------------------------------------
# sha256 stability across runs
# ---------------------------------------------------------------------------


def test_extract_paper_sha256_stable_across_runs(repo_paths: tuple[Path, Path]) -> None:
    datasets_repo, _materials_dir = repo_paths
    _write_fixture_pdf(datasets_repo / "studies" / STUDY / "paper.pdf")

    first = paper.extract_paper(STUDY)
    second = paper.extract_paper(STUDY)

    assert first.sha256 is not None
    assert first.sha256 == second.sha256


# ---------------------------------------------------------------------------
# not_found: no paper.pdf anywhere
# ---------------------------------------------------------------------------


def test_extract_paper_not_found_when_no_pdf_anywhere(repo_paths: tuple[Path, Path]) -> None:
    status = paper.extract_paper(STUDY)

    assert status.status == "not_found"
    assert status.pdf_path is None
    assert status.sha256 is None
    assert status.retrieved_from is None
    assert status.candidates_tried == []
    assert status.n_pages is None
    assert status.text_quality is None
    assert status.notes  # a note pointing at the paper-finder agent
    assert any("paper-finder" in note for note in status.notes)


def test_cli_extract_paper_not_found_writes_artifact_and_nonzero_exit(
    repo_paths: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["extract-paper", STUDY])

    assert exit_code == 1
    artifact_path = tmp_path / "runs" / STUDY / "paper_status.json"
    data = schemas.PaperStatus.model_validate_json(artifact_path.read_text())
    assert data.status == "not_found"


# ---------------------------------------------------------------------------
# materials-fallback: paper.pdf absent from studies/, present in materials/
# ---------------------------------------------------------------------------


def test_extract_paper_falls_back_to_materials_dir(repo_paths: tuple[Path, Path]) -> None:
    _datasets_repo, materials_dir = repo_paths
    _write_fixture_pdf(materials_dir / STUDY / "paper.pdf")

    status = paper.extract_paper(STUDY)

    assert status.status == "found_local"
    assert status.retrieved_from == f"materials/{STUDY}/paper.pdf"
    assert status.pdf_path == f"materials/{STUDY}/paper.pdf"
    assert status.n_pages == 3
    text_dir = materials_dir / STUDY / "paper_text"
    assert (text_dir / "page_001.txt").exists()


def test_extract_paper_prefers_studies_over_materials_when_both_present(
    repo_paths: tuple[Path, Path],
) -> None:
    datasets_repo, materials_dir = repo_paths
    _write_fixture_pdf(datasets_repo / "studies" / STUDY / "paper.pdf")
    # A stray/older materials copy should not win over the studies copy.
    (materials_dir / STUDY).mkdir(parents=True)
    (materials_dir / STUDY / "paper.pdf").write_bytes(b"%PDF-not-a-real-pdf")

    status = paper.extract_paper(STUDY)

    assert status.retrieved_from == f"studies/{STUDY}/paper.pdf"


# ---------------------------------------------------------------------------
# idempotent re-run: overwrites paper_text/ and paper_status.json cleanly
# ---------------------------------------------------------------------------


def test_extract_paper_idempotent_rerun_overwrites_cleanly(
    repo_paths: tuple[Path, Path],
) -> None:
    datasets_repo, materials_dir = repo_paths
    _write_fixture_pdf(datasets_repo / "studies" / STUDY / "paper.pdf")

    first = paper.extract_paper(STUDY)
    # Plant a stale leftover page file that a naive re-run might not clean up.
    stale = materials_dir / STUDY / "paper_text" / "page_999.txt"
    stale.write_text("stale leftover from a previous, differently-shaped PDF")

    second = paper.extract_paper(STUDY)

    assert second.n_pages == first.n_pages == 3
    text_dir = materials_dir / STUDY / "paper_text"
    assert not stale.exists()
    assert sorted(p.name for p in text_dir.iterdir()) == [
        "page_001.txt",
        "page_002.txt",
        "page_003.txt",
    ]
    assert second.text_quality == first.text_quality
    assert second.sha256 == first.sha256
