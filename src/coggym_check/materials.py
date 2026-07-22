"""Materials download: Stage 2's deterministic half of `materials_manifest.json`.

Why this module exists (task-7 brief): a `materials-scout` agent discovers
candidate OSF nodes, GitHub repos, and bare URLs for a study and writes them
into `materials_manifest.json` as `MaterialsSource` entries with
`download_status="pending"`. This module never decides *what* to download —
it only consumes an already-written manifest and deterministically fetches
each pending source, back-filling `local_path`, `sha256_or_commit`, and
`download_status` in place. Judgment (is this really the official
materials repo? is this OSF node worth following?) stays with the agent;
everything here is a pure function of the manifest plus network responses.

Directory layout written under `materials/<study>/`:
- `osf/<node_id>/...`        -- OSF storage tree, folder structure preserved
- `github/<repo-name>/...`   -- `git clone --depth 1` of a GitHub (or any
                                 git-clonable) URL
- `other/<filename>`         -- a single `other_url` GET, filename taken
                                 from the URL's path basename

`sha256_or_commit` convention (pre-answered clarification, documented here
since it isn't obvious from the schema alone):
- `github` sources: the cloned repo's HEAD commit sha (`git rev-parse HEAD`).
- `osf`/`other_url` sources: the sha256 of a "manifest of files" string --
  every downloaded file's `<relpath>:<file-sha256>` line, sorted, newline
  joined, then hashed. This makes the recorded digest a single stable value
  that changes if *any* downloaded byte or file layout changes, without
  needing a list-valued field on `MaterialsSource`. `other_url` sources
  happen to always produce exactly one line, but the same helper is reused
  for consistency rather than hashing the raw file bytes directly.

Size cap (pre-answered clarification): `_MAX_SOURCE_BYTES` (500MB) is a
per-source cumulative cap. Content-Length is checked (when the server sends
one) before each file's body is streamed to disk; if a file would push the
running total over the cap, that file is not written, the source is marked
`skipped_too_large`, and files already downloaded for that source are kept
in place. A file whose actual streamed size (server omitted or lied about
Content-Length) crosses the cap mid-stream is caught by the same check
applied incrementally and the partial file is removed.

Network timeouts (pre-answered clarification): every `requests` call uses
`timeout=_TIMEOUT_S` (30s); a timeout is treated exactly like any other
request failure -- the source is marked `failed`, nothing raises out of
this module.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import requests

from coggym_check import config, schemas

#: Per-source cumulative download cap (pre-answered clarification). A file
#: that would push a source's running total over this line is not written;
#: the source is marked `skipped_too_large` and downloading stops for it.
_MAX_SOURCE_BYTES = 500 * 1024 * 1024

#: Every `requests` call's timeout, in seconds (pre-answered clarification).
_TIMEOUT_S = 30

#: `git clone`'s subprocess timeout. Not a network-request timeout (git has
#: its own transport), so kept separate and more generous -- this is an
#: engineering safety valve against a hung clone, not a scientific choice.
_GIT_CLONE_TIMEOUT_S = 120

_OSF_API_BASE = "https://api.osf.io/v2"

#: Matches `osf.io/<5-char-id>` with or without a scheme/trailing slash.
_OSF_NODE_RE = re.compile(r"(?:https?://)?osf\.io/([A-Za-z0-9]{5})/?")

_CHUNK_SIZE = 65536


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _manifest_sha256(entries: list[tuple[str, str]]) -> str:
    """sha256 of the sorted `<relpath>:<file-sha256>` lines -- see module
    docstring's `sha256_or_commit` convention."""
    lines = sorted(f"{relpath}:{file_sha256}" for relpath, file_sha256 in entries)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _download_within_cap(
    url: str, dest_path: Path, running_total: int
) -> tuple[int, str] | None:
    """Stream `url` to `dest_path`, returning `(bytes_written, sha256_hex)`.

    Returns `None` (writing nothing, or removing a partial file) if this
    download would push `running_total` over `_MAX_SOURCE_BYTES` -- checked
    against the response's `Content-Length` header up front when present,
    and incrementally against actual bytes streamed either way (covering
    servers that omit or understate `Content-Length`).
    """
    resp = requests.get(url, stream=True, timeout=_TIMEOUT_S)
    resp.raise_for_status()

    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if declared_size is not None and running_total + declared_size > _MAX_SOURCE_BYTES:
            resp.close()
            return None

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    total = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
            if not chunk:
                continue
            total += len(chunk)
            if running_total + total > _MAX_SOURCE_BYTES:
                f.close()
                dest_path.unlink(missing_ok=True)
                return None
            f.write(chunk)
            hasher.update(chunk)
    return total, hasher.hexdigest()


# ---------------------------------------------------------------------------
# OSF
# ---------------------------------------------------------------------------


def _extract_osf_node_id(url: str) -> str | None:
    match = _OSF_NODE_RE.search(url)
    return match.group(1) if match else None


def _osf_list_page(url: str) -> dict:
    resp = requests.get(url, timeout=_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


def _osf_walk_files(start_url: str) -> list[tuple[str, dict]]:
    """Recursively list an OSF storage tree, returning `(relpath, file_entry)`
    pairs. Folders are recursed into via each entry's own files listing href
    (`relationships.files.links.related.href`); each listing (folder or
    root) is paginated via `links.next`.
    """
    results: list[tuple[str, dict]] = []

    def _walk(url: str, prefix: str) -> None:
        next_url: str | None = url
        while next_url:
            data = _osf_list_page(next_url)
            for entry in data["data"]:
                attrs = entry.get("attributes", {})
                name = attrs.get("name", "unnamed")
                relpath = f"{prefix}{name}"
                if attrs.get("kind") == "folder":
                    folder_url = entry["relationships"]["files"]["links"]["related"]["href"]
                    _walk(folder_url, f"{relpath}/")
                else:
                    results.append((relpath, entry))
            next_url = (data.get("links") or {}).get("next")

    _walk(start_url, "")
    return results


def _download_osf_source(
    source: schemas.MaterialsSource, study: str
) -> tuple[schemas.MaterialsSource, list[str]]:
    notes: list[str] = []
    node_id = _extract_osf_node_id(source.url)
    if node_id is None:
        notes.append(f"osf source {source.url}: could not extract a node id from URL")
        return source.model_copy(update={"download_status": "failed"}), notes

    listing_url = f"{_OSF_API_BASE}/nodes/{node_id}/files/osfstorage/"
    try:
        files = _osf_walk_files(listing_url)
    except (requests.RequestException, KeyError, ValueError) as exc:
        notes.append(f"osf source {source.url}: failed to list files ({exc})")
        return source.model_copy(update={"download_status": "failed"}), notes

    dest_dir = config.materials_dir() / study / "osf" / node_id
    downloaded: list[tuple[str, str]] = []
    total_bytes = 0
    skipped_too_large = False

    for relpath, entry in sorted(files, key=lambda pair: pair[0]):
        download_url = (entry.get("links") or {}).get("download")
        if not download_url:
            notes.append(f"osf source {source.url}: {relpath} has no download link, skipping")
            continue
        try:
            result = _download_within_cap(download_url, dest_dir / relpath, total_bytes)
        except requests.RequestException as exc:
            notes.append(f"osf source {source.url}: failed to download {relpath} ({exc})")
            return source.model_copy(update={"download_status": "failed"}), notes
        if result is None:
            skipped_too_large = True
            notes.append(
                f"osf source {source.url}: stopped at {relpath} -- would exceed "
                f"the {_MAX_SOURCE_BYTES}-byte per-source cap"
            )
            break
        size, sha256_hex = result
        total_bytes += size
        downloaded.append((relpath, sha256_hex))

    if not downloaded:
        notes.append(f"osf source {source.url}: no files downloaded")
        return source.model_copy(update={"download_status": "failed"}), notes

    status = "skipped_too_large" if skipped_too_large else "ok"
    return (
        source.model_copy(
            update={
                "download_status": status,
                "local_path": f"materials/{study}/osf/{node_id}",
                "sha256_or_commit": _manifest_sha256(downloaded),
            }
        ),
        notes,
    )


# ---------------------------------------------------------------------------
# GitHub (or any git-clonable URL)
# ---------------------------------------------------------------------------


def _repo_name_from_url(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name or "repo"


def _git_head(dest_dir: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(dest_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=True,
    )
    return result.stdout.strip()


def _download_github_source(
    source: schemas.MaterialsSource, study: str
) -> tuple[schemas.MaterialsSource, list[str]]:
    notes: list[str] = []
    repo_name = _repo_name_from_url(source.url)
    dest_dir = config.materials_dir() / study / "github" / repo_name
    local_path = f"materials/{study}/github/{repo_name}"

    if dest_dir.exists() and any(dest_dir.iterdir()):
        # Pre-answered clarification: a non-empty existing target dir means
        # a previous run already cloned this (possibly crashing before it
        # could back-fill the manifest) -- treat as already-downloaded
        # rather than re-cloning (git would refuse to clone into it anyway).
        try:
            head = _git_head(dest_dir)
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
            notes.append(
                f"github source {source.url}: existing dir {dest_dir} is not a "
                f"readable git repo ({exc})"
            )
            return source.model_copy(update={"download_status": "failed"}), notes
        notes.append(f"github source {source.url}: already present at {dest_dir}")
        return (
            source.model_copy(
                update={"download_status": "ok", "local_path": local_path, "sha256_or_commit": head}
            ),
            notes,
        )

    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", source.url, str(dest_dir)],
            capture_output=True,
            text=True,
            timeout=_GIT_CLONE_TIMEOUT_S,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
        notes.append(f"github source {source.url}: clone failed ({exc})")
        return source.model_copy(update={"download_status": "failed"}), notes

    try:
        head = _git_head(dest_dir)
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
        notes.append(f"github source {source.url}: cloned but rev-parse HEAD failed ({exc})")
        return source.model_copy(update={"download_status": "failed"}), notes

    return (
        source.model_copy(
            update={"download_status": "ok", "local_path": local_path, "sha256_or_commit": head}
        ),
        notes,
    )


# ---------------------------------------------------------------------------
# other_url (single-file GET)
# ---------------------------------------------------------------------------


def _download_other_url_source(
    source: schemas.MaterialsSource, study: str
) -> tuple[schemas.MaterialsSource, list[str]]:
    notes: list[str] = []
    filename = Path(urlparse(source.url).path).name or "download.bin"
    dest_dir = config.materials_dir() / study / "other"

    try:
        result = _download_within_cap(source.url, dest_dir / filename, running_total=0)
    except requests.RequestException as exc:
        notes.append(f"other_url source {source.url}: failed to download ({exc})")
        return source.model_copy(update={"download_status": "failed"}), notes

    if result is None:
        notes.append(
            f"other_url source {source.url}: exceeds the {_MAX_SOURCE_BYTES}-byte "
            "per-source cap"
        )
        return source.model_copy(update={"download_status": "skipped_too_large"}), notes

    _size, sha256_hex = result
    return (
        source.model_copy(
            update={
                "download_status": "ok",
                "local_path": f"materials/{study}/other/{filename}",
                "sha256_or_commit": _manifest_sha256([(filename, sha256_hex)]),
            }
        ),
        notes,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_DOWNLOADERS = {
    "osf": _download_osf_source,
    "github": _download_github_source,
    "other_url": _download_other_url_source,
}


def download_manifest(manifest: schemas.MaterialsManifest) -> schemas.MaterialsManifest:
    """Download every `pending` source in `manifest`, returning an updated copy.

    Re-run semantics (pre-answered clarification): only `pending` sources are
    attempted -- `ok`/`failed`/`skipped_too_large` sources are left exactly
    as they are, so the materials-scout agent can flip a `failed` source back
    to `pending` to retry it without disturbing everything else. Failures
    never raise: every downloader below catches its own exceptions and
    returns a `failed`-status copy plus an explanatory note instead.
    """
    new_sources: list[schemas.MaterialsSource] = []
    new_notes = list(manifest.notes)

    for source in manifest.sources:
        if source.download_status != "pending":
            new_sources.append(source)
            continue
        downloader = _DOWNLOADERS[source.kind]
        updated_source, notes = downloader(source, manifest.study)
        new_sources.append(updated_source)
        new_notes.extend(notes)

    return manifest.model_copy(update={"sources": new_sources, "notes": new_notes})
