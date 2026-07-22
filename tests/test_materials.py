"""Tests for coggym_check.materials — OSF/GitHub/other_url downloading and
the `download` CLI subcommand.

No real network anywhere (constraints.md hard rule #8): `requests.get` is
monkeypatched to a small in-memory fixture server (`FakeResponse` +
`_install_fake_requests`) recording canned OSF API JSON (paginated node
listing, a folder recursion, and file downloads). The GitHub path is
exercised against a real local git repo cloned via a `file://` URL (no
network, but real `git` subprocess calls), per the task brief.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
import requests

from coggym_check import cli, config, materials, schemas

STUDY = "TestStudy2020Mini"


# ---------------------------------------------------------------------------
# Fake requests plumbing (OSF + other_url tests)
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for `requests.Response` covering what materials.py uses."""

    def __init__(
        self,
        *,
        json_data: dict | None = None,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> None:
        self._json_data = json_data
        self._content = content
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error for url")

    def json(self) -> dict:
        assert self._json_data is not None
        return self._json_data

    def iter_content(self, chunk_size: int = 65536):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def close(self) -> None:
        pass


@pytest.fixture()
def fake_requests(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeResponse | Exception]:
    """Install a `requests.get` stub dispatching on exact URL.

    Returns the (initially empty) routing dict; tests populate it with
    `FakeResponse` instances or exceptions to raise, keyed by URL.
    """
    routes: dict[str, FakeResponse | Exception] = {}

    def fake_get(url: str, *, timeout=None, stream: bool = False):  # noqa: ANN001
        if url not in routes:
            raise AssertionError(f"unexpected request to {url}")
        resp = routes[url]
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(requests, "get", fake_get)
    return routes


@pytest.fixture()
def materials_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "materials"
    monkeypatch.setattr(config, "materials_dir", lambda: root)
    return root


def _osf_entry(name: str, *, kind: str, download: str | None = None, related: str | None = None) -> dict:
    entry: dict = {"attributes": {"name": name, "kind": kind}}
    if kind == "file":
        entry["links"] = {"download": download}
    else:
        entry["relationships"] = {"files": {"links": {"related": {"href": related}}}}
    return entry


def _content_response(content: bytes) -> FakeResponse:
    return FakeResponse(content=content, headers={"Content-Length": str(len(content))})


# ---------------------------------------------------------------------------
# OSF: paginated root listing + folder recursion + downloads
# ---------------------------------------------------------------------------

NODE_ID = "abc12"
ROOT_URL = f"https://api.osf.io/v2/nodes/{NODE_ID}/files/osfstorage/"
ROOT_PAGE_2_URL = ROOT_URL + "?page=2"
FOLDER_URL = f"https://api.osf.io/v2/nodes/{NODE_ID}/files/osfstorage/folder1/"
DL_INSTRUCTIONS = "https://osf.io/download/instructions.pdf"
DL_README = "https://osf.io/download/readme.txt"
DL_IMAGE = "https://osf.io/download/image1.png"


def _wire_osf_happy_path(fake_requests: dict) -> dict[str, bytes]:
    """Wire a 2-page root listing (file + folder) + 1-page folder listing."""
    contents = {
        "instructions.pdf": b"instructions body",
        "readme.txt": b"readme body, a bit longer than the first",
        "image1.png": b"fake png bytes",
    }
    fake_requests[ROOT_URL] = FakeResponse(
        json_data={
            "data": [
                _osf_entry("instructions.pdf", kind="file", download=DL_INSTRUCTIONS),
                _osf_entry("stimuli", kind="folder", related=FOLDER_URL),
            ],
            "links": {"next": ROOT_PAGE_2_URL},
        }
    )
    fake_requests[ROOT_PAGE_2_URL] = FakeResponse(
        json_data={
            "data": [_osf_entry("readme.txt", kind="file", download=DL_README)],
            "links": {"next": None},
        }
    )
    fake_requests[FOLDER_URL] = FakeResponse(
        json_data={
            "data": [_osf_entry("image1.png", kind="file", download=DL_IMAGE)],
            "links": {"next": None},
        }
    )
    fake_requests[DL_INSTRUCTIONS] = _content_response(contents["instructions.pdf"])
    fake_requests[DL_README] = _content_response(contents["readme.txt"])
    fake_requests[DL_IMAGE] = _content_response(contents["image1.png"])
    return contents


def _osf_source(url: str = f"https://osf.io/{NODE_ID}/") -> schemas.MaterialsSource:
    return schemas.MaterialsSource(
        kind="osf",
        url=url,
        relation="official_materials",
        evidence="linked from paper abstract",
        local_path=None,
        download_status="pending",
        sha256_or_commit=None,
    )


def test_osf_download_writes_directory_layout_and_backfills_manifest(
    fake_requests: dict, materials_root: Path
) -> None:
    contents = _wire_osf_happy_path(fake_requests)
    manifest = schemas.MaterialsManifest(
        study=STUDY, status="found", searches=[], sources=[_osf_source()], notes=[]
    )

    updated = materials.download_manifest(manifest)

    assert len(updated.sources) == 1
    source = updated.sources[0]
    assert source.download_status == "ok"
    assert source.local_path == f"materials/{STUDY}/osf/{NODE_ID}"

    base = materials_root / STUDY / "osf" / NODE_ID
    assert (base / "instructions.pdf").read_bytes() == contents["instructions.pdf"]
    assert (base / "readme.txt").read_bytes() == contents["readme.txt"]
    assert (base / "stimuli" / "image1.png").read_bytes() == contents["image1.png"]

    # sha256_or_commit is the manifest-of-files convention, not a raw file hash.
    expected_lines = sorted(
        f"{relpath}:{hashlib.sha256(data).hexdigest()}"
        for relpath, data in [
            ("instructions.pdf", contents["instructions.pdf"]),
            ("readme.txt", contents["readme.txt"]),
            ("stimuli/image1.png", contents["image1.png"]),
        ]
    )
    expected = hashlib.sha256("\n".join(expected_lines).encode("utf-8")).hexdigest()
    assert source.sha256_or_commit == expected

    # Whole manifest still validates.
    schemas.MaterialsManifest.model_validate_json(updated.model_dump_json())


def test_osf_bare_url_without_scheme_also_resolves_node_id(
    fake_requests: dict, materials_root: Path
) -> None:
    _wire_osf_happy_path(fake_requests)
    manifest = schemas.MaterialsManifest(
        study=STUDY,
        status="found",
        searches=[],
        sources=[_osf_source(url=f"osf.io/{NODE_ID}")],
        notes=[],
    )

    updated = materials.download_manifest(manifest)

    assert updated.sources[0].download_status == "ok"


# ---------------------------------------------------------------------------
# OSF: size cap via Content-Length
# ---------------------------------------------------------------------------


def test_osf_size_cap_stops_source_and_keeps_already_downloaded_files(
    fake_requests: dict, materials_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Small cap so a two-file source trips it deterministically.
    monkeypatch.setattr(materials, "_MAX_SOURCE_BYTES", 20)

    small_url = "https://api.osf.io/v2/nodes/cap01/files/osfstorage/"
    dl_small = "https://osf.io/download/small.txt"
    dl_big = "https://osf.io/download/big.txt"
    small_content = b"0123456789"  # 10 bytes, fits under cap of 20
    big_content = b"x" * 30  # would push 10+30=40 > 20

    fake_requests[small_url] = FakeResponse(
        json_data={
            "data": [
                _osf_entry("small.txt", kind="file", download=dl_small),
                _osf_entry("zzz_big.txt", kind="file", download=dl_big),
            ],
            "links": {"next": None},
        }
    )
    fake_requests[dl_small] = _content_response(small_content)
    fake_requests[dl_big] = _content_response(big_content)

    manifest = schemas.MaterialsManifest(
        study=STUDY,
        status="found",
        searches=[],
        sources=[_osf_source(url="https://osf.io/cap01/")],
        notes=[],
    )

    updated = materials.download_manifest(manifest)

    source = updated.sources[0]
    assert source.download_status == "skipped_too_large"
    base = materials_root / STUDY / "osf" / "cap01"
    assert (base / "small.txt").read_bytes() == small_content
    assert not (base / "zzz_big.txt").exists()
    assert any("cap" in note.lower() or "exceed" in note.lower() for note in updated.notes)


# ---------------------------------------------------------------------------
# OSF: 404 / timeout -> failed, never raises
# ---------------------------------------------------------------------------


def test_osf_404_on_listing_marks_failed_without_raising(
    fake_requests: dict, materials_root: Path
) -> None:
    url = "https://api.osf.io/v2/nodes/dead1/files/osfstorage/"
    fake_requests[url] = FakeResponse(status_code=404)
    manifest = schemas.MaterialsManifest(
        study=STUDY,
        status="found",
        searches=[],
        sources=[_osf_source(url="https://osf.io/dead1/")],
        notes=[],
    )

    updated = materials.download_manifest(manifest)

    assert updated.sources[0].download_status == "failed"
    assert updated.sources[0].local_path is None
    assert updated.sources[0].sha256_or_commit is None


def test_osf_timeout_on_listing_marks_failed_without_raising(
    fake_requests: dict, materials_root: Path
) -> None:
    url = "https://api.osf.io/v2/nodes/time1/files/osfstorage/"
    fake_requests[url] = requests.exceptions.Timeout("timed out")
    manifest = schemas.MaterialsManifest(
        study=STUDY,
        status="found",
        searches=[],
        sources=[_osf_source(url="https://osf.io/time1/")],
        notes=[],
    )

    updated = materials.download_manifest(manifest)

    assert updated.sources[0].download_status == "failed"


# ---------------------------------------------------------------------------
# other_url
# ---------------------------------------------------------------------------


def test_other_url_downloads_single_file_and_backfills_manifest(
    fake_requests: dict, materials_root: Path
) -> None:
    url = "https://example.com/path/to/stimuli.zip"
    content = b"zip file bytes go here"
    fake_requests[url] = _content_response(content)
    source = schemas.MaterialsSource(
        kind="other_url",
        url=url,
        relation="official_materials",
        evidence="linked in supplementary materials",
        local_path=None,
        download_status="pending",
        sha256_or_commit=None,
    )
    manifest = schemas.MaterialsManifest(
        study=STUDY, status="found", searches=[], sources=[source], notes=[]
    )

    updated = materials.download_manifest(manifest)

    result = updated.sources[0]
    assert result.download_status == "ok"
    assert result.local_path == f"materials/{STUDY}/other/stimuli.zip"
    dest = materials_root / STUDY / "other" / "stimuli.zip"
    assert dest.read_bytes() == content
    expected = hashlib.sha256(
        f"stimuli.zip:{hashlib.sha256(content).hexdigest()}".encode("utf-8")
    ).hexdigest()
    assert result.sha256_or_commit == expected


def test_other_url_404_marks_failed_without_raising(
    fake_requests: dict, materials_root: Path
) -> None:
    url = "https://example.com/missing.zip"
    fake_requests[url] = FakeResponse(status_code=404)
    source = schemas.MaterialsSource(
        kind="other_url",
        url=url,
        relation="uncertain",
        evidence="",
        local_path=None,
        download_status="pending",
        sha256_or_commit=None,
    )
    manifest = schemas.MaterialsManifest(
        study=STUDY, status="found", searches=[], sources=[source], notes=[]
    )

    updated = materials.download_manifest(manifest)

    assert updated.sources[0].download_status == "failed"


def test_other_url_falls_back_to_download_bin_when_no_basename(
    fake_requests: dict, materials_root: Path
) -> None:
    url = "https://example.com/"
    content = b"some bytes"
    fake_requests[url] = _content_response(content)
    source = schemas.MaterialsSource(
        kind="other_url",
        url=url,
        relation="uncertain",
        evidence="",
        local_path=None,
        download_status="pending",
        sha256_or_commit=None,
    )
    manifest = schemas.MaterialsManifest(
        study=STUDY, status="found", searches=[], sources=[source], notes=[]
    )

    updated = materials.download_manifest(manifest)

    assert updated.sources[0].local_path == f"materials/{STUDY}/other/download.bin"


# ---------------------------------------------------------------------------
# GitHub: local bare repo cloned via file:// URL
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_bare_repo(tmp_path: Path, name: str = "study-materials") -> Path:
    """Create a real local git repo with one commit, then a bare clone of it."""
    work = tmp_path / "work"
    work.mkdir()
    _run_git(["init", "-q"], cwd=work)
    _run_git(["config", "user.email", "test@example.com"], cwd=work)
    _run_git(["config", "user.name", "Test"], cwd=work)
    (work / "README.md").write_text("materials repo\n")
    _run_git(["add", "."], cwd=work)
    _run_git(["commit", "-q", "-m", "init"], cwd=work)

    bare = tmp_path / f"{name}.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    return bare


def _github_source(url: str) -> schemas.MaterialsSource:
    return schemas.MaterialsSource(
        kind="github",
        url=url,
        relation="official_code",
        evidence="linked from README",
        local_path=None,
        download_status="pending",
        sha256_or_commit=None,
    )


def test_github_clone_records_head_commit_and_backfills_manifest(
    tmp_path: Path, materials_root: Path
) -> None:
    bare = _make_bare_repo(tmp_path)
    manifest = schemas.MaterialsManifest(
        study=STUDY,
        status="found",
        searches=[],
        sources=[_github_source(f"file://{bare}")],
        notes=[],
    )

    updated = materials.download_manifest(manifest)

    source = updated.sources[0]
    assert source.download_status == "ok"
    assert source.local_path == f"materials/{STUDY}/github/study-materials"
    clone_dir = materials_root / STUDY / "github" / "study-materials"
    assert (clone_dir / "README.md").exists()

    expected_head = subprocess.run(
        ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert source.sha256_or_commit == expected_head
    assert len(source.sha256_or_commit) == 40  # full sha, not abbreviated


def test_github_clone_failure_marks_failed_without_raising(
    tmp_path: Path, materials_root: Path
) -> None:
    nonexistent = tmp_path / "does-not-exist.git"
    manifest = schemas.MaterialsManifest(
        study=STUDY,
        status="found",
        searches=[],
        sources=[_github_source(f"file://{nonexistent}")],
        notes=[],
    )

    updated = materials.download_manifest(manifest)

    assert updated.sources[0].download_status == "failed"


def test_github_already_present_nonempty_dir_treated_as_already_downloaded(
    tmp_path: Path, materials_root: Path
) -> None:
    bare = _make_bare_repo(tmp_path)
    # Simulate a previous run that already cloned this (e.g. crashed before
    # back-filling the manifest): pre-populate the destination for real.
    dest = materials_root / STUDY / "github" / "study-materials"
    dest.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{bare}", str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    pre_existing_head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    manifest = schemas.MaterialsManifest(
        study=STUDY,
        status="found",
        searches=[],
        sources=[_github_source(f"file://{bare}")],
        notes=[],
    )

    updated = materials.download_manifest(manifest)

    source = updated.sources[0]
    # If our code hadn't special-cased this, `git clone` into a non-empty
    # dir would itself fail and this would be "failed", not "ok" -- so this
    # assertion also validates the already-present short-circuit exists.
    assert source.download_status == "ok"
    assert source.sha256_or_commit == pre_existing_head
    assert any("already present" in note for note in updated.notes)


# ---------------------------------------------------------------------------
# Re-run semantics: non-pending sources are left alone
# ---------------------------------------------------------------------------


def test_non_pending_sources_are_left_untouched_on_rerun(
    fake_requests: dict, materials_root: Path
) -> None:
    already_ok = schemas.MaterialsSource(
        kind="osf",
        url="https://osf.io/zzzzz/",
        relation="official_materials",
        evidence="",
        local_path=f"materials/{STUDY}/osf/zzzzz",
        download_status="ok",
        sha256_or_commit="preexisting-hash",
    )
    already_failed = schemas.MaterialsSource(
        kind="other_url",
        url="https://example.com/gone.zip",
        relation="uncertain",
        evidence="",
        local_path=None,
        download_status="failed",
        sha256_or_commit=None,
    )
    already_skipped = schemas.MaterialsSource(
        kind="osf",
        url="https://osf.io/yyyyy/",
        relation="uncertain",
        evidence="",
        local_path=f"materials/{STUDY}/osf/yyyyy",
        download_status="skipped_too_large",
        sha256_or_commit="partial-hash",
    )
    manifest = schemas.MaterialsManifest(
        study=STUDY,
        status="partial",
        searches=[],
        sources=[already_ok, already_failed, already_skipped],
        notes=[],
    )

    updated = materials.download_manifest(manifest)

    assert updated.sources == [already_ok, already_failed, already_skipped]
    # No requests should have been made at all (routes dict stays unused).
    assert fake_requests == {}


# ---------------------------------------------------------------------------
# CLI `download` subcommand
# ---------------------------------------------------------------------------


def test_cli_download_writes_backfilled_manifest_and_exits_0(
    fake_requests: dict, materials_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = _wire_osf_happy_path(fake_requests)
    del contents
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    run_dir = tmp_path / "runs" / STUDY
    run_dir.mkdir(parents=True)
    manifest = schemas.MaterialsManifest(
        study=STUDY, status="found", searches=[], sources=[_osf_source()], notes=[]
    )
    (run_dir / "materials_manifest.json").write_text(manifest.model_dump_json(indent=2))

    exit_code = cli.main(["download", STUDY])

    assert exit_code == 0
    updated = schemas.MaterialsManifest.model_validate_json(
        (run_dir / "materials_manifest.json").read_text()
    )
    assert updated.sources[0].download_status == "ok"


def test_cli_download_exits_1_when_all_pending_sources_fail(
    fake_requests: dict, materials_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_requests["https://api.osf.io/v2/nodes/badd1/files/osfstorage/"] = FakeResponse(
        status_code=404
    )
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    run_dir = tmp_path / "runs" / STUDY
    run_dir.mkdir(parents=True)
    manifest = schemas.MaterialsManifest(
        study=STUDY,
        status="found",
        searches=[],
        sources=[_osf_source(url="https://osf.io/badd1/")],
        notes=[],
    )
    (run_dir / "materials_manifest.json").write_text(manifest.model_dump_json(indent=2))

    exit_code = cli.main(["download", STUDY])

    assert exit_code == 1


def test_cli_download_exits_0_when_no_pending_sources(
    materials_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")
    run_dir = tmp_path / "runs" / STUDY
    run_dir.mkdir(parents=True)
    manifest = schemas.MaterialsManifest(study=STUDY, status="none_found", searches=[], sources=[], notes=[])
    (run_dir / "materials_manifest.json").write_text(manifest.model_dump_json(indent=2))

    exit_code = cli.main(["download", STUDY])

    assert exit_code == 0


def test_cli_download_missing_manifest_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "runs_dir", lambda: tmp_path / "runs")

    exit_code = cli.main(["download", STUDY])

    assert exit_code == 1
