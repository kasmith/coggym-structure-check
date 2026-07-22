"""Paper acquisition (local-only) + text extraction: Stage 1's deterministic
half of ``paper_status.json``.

Why local-only here: this module (and its ``extract-paper`` CLI subcommand)
never touches the network. If ``paper.pdf`` can't be found on disk, it writes
``status="not_found"`` and stops — going out to find an open-access copy
online is the ``paper-finder`` agent's job (Task 9), not this deterministic
stage's. Keeping that boundary hard means `extract_paper` stays trivially
unit-testable (constraints.md hard rule #8: no network in unit tests) and
re-runnable without rate-limit or flakiness concerns.

Resolution order for ``paper.pdf`` (pre-answered clarification, documented
here since it's not obvious from the schema alone):

1. ``datasets_repo()/studies/<study>/paper.pdf`` — the paper as shipped in
   the dataset itself.
2. ``materials_dir()/<study>/paper.pdf`` — a copy placed there by the
   paper-finder agent on a previous run (e.g. an online-sourced PDF), used
   even when the study folder in the datasets repo has no ``paper.pdf`` at
   all.
3. Otherwise: ``not_found``.

Note on ``--commit``: unlike ``snapshot``/``lint``, extraction always reads
``paper.pdf`` from the datasets repo's *working tree*, never a pinned
commit. Papers aren't meaningfully versioned per-commit in this dataset (a
study's PDF doesn't change across commits the way its JSON structure might),
so `extract_paper` takes no `commit` parameter; the CLI's `--commit` only
selects which run dir (`runs/<study>` vs `runs/<study>@<shortsha>`) the
resulting `paper_status.json` is written into.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pymupdf

from coggym_check import config, schemas

#: A page counts as "extractable" once its text crosses this length —
#: matches the task brief's `text_quality` definition exactly (fraction of
#: pages with >=200 extractable characters).
_MIN_EXTRACTABLE_CHARS = 200


def _not_found_status(study: str) -> schemas.PaperStatus:
    return schemas.PaperStatus(
        study=study,
        status="not_found",
        pdf_path=None,
        sha256=None,
        retrieved_from=None,
        candidates_tried=[],
        n_pages=None,
        text_quality=None,
        # No PDF at all to judge extractability from; treat as "needs a
        # human/agent to read the eventual PDF directly" until one is found.
        needs_direct_pdf_read=True,
        notes=[
            f"paper.pdf not found under studies/{study}/ or materials/{study}/; "
            "run the paper-finder agent to locate an online (or paywalled) "
            "copy -- this CLI does not fetch the web.",
        ],
    )


def _resolve_source_pdf(study: str) -> tuple[Path, str] | None:
    """Return `(path, retrieved_from)` for the first `paper.pdf` found, per
    the module docstring's resolution order, or `None` if neither exists."""
    studies_pdf = config.datasets_repo() / "studies" / study / "paper.pdf"
    if studies_pdf.exists():
        return studies_pdf, f"studies/{study}/paper.pdf"

    materials_pdf = config.materials_dir() / study / "paper.pdf"
    if materials_pdf.exists():
        return materials_pdf, f"materials/{study}/paper.pdf"

    return None


def extract_paper(study: str) -> schemas.PaperStatus:
    """Locate `study`'s `paper.pdf`, copy it into `materials/<study>/`, and
    extract per-page text.

    Idempotent by construction: `paper_text/` is removed and rebuilt from
    scratch each call (so a re-run after a differently-paginated PDF never
    leaves stale page files behind), and the destination `paper.pdf` is
    simply overwritten (or, in the materials-fallback case where the source
    *is* the destination, left as-is -- see the `source == dest_pdf` guard
    below, which also avoids `shutil.copyfile`'s same-file error).

    If the PDF is unreadable (corrupt, encrypted, etc.), writes a structured
    not_found status instead of crashing, cleaning up any partial materials.
    """
    resolved = _resolve_source_pdf(study)
    if resolved is None:
        return _not_found_status(study)
    source_pdf, retrieved_from = resolved

    dest_dir = config.materials_dir() / study
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_pdf = dest_dir / "paper.pdf"
    if source_pdf != dest_pdf:
        shutil.copyfile(source_pdf, dest_pdf)

    text_dir = dest_dir / "paper_text"
    if text_dir.exists():
        shutil.rmtree(text_dir)
    text_dir.mkdir(parents=True)

    sha256 = hashlib.sha256(dest_pdf.read_bytes()).hexdigest()

    try:
        doc = pymupdf.open(dest_pdf)
    except (pymupdf.FileDataError, pymupdf.EmptyFileError) as e:
        # PDF is corrupt, encrypted, or otherwise unreadable. Clean up
        # partial state and return a structured not_found status.
        if text_dir.exists():
            shutil.rmtree(text_dir)
        # Only delete the dest PDF if it was copied in this call (not the source itself).
        if source_pdf != dest_pdf and dest_pdf.exists():
            dest_pdf.unlink()
        return schemas.PaperStatus(
            study=study,
            status="not_found",
            pdf_path=None,
            sha256=None,
            retrieved_from=None,
            candidates_tried=[],
            n_pages=None,
            text_quality=None,
            needs_direct_pdf_read=True,
            notes=[f"paper.pdf present but unreadable: {e}"],
        )

    try:
        n_pages = doc.page_count
        n_good_pages = 0
        for i, page in enumerate(doc, start=1):
            text = page.get_text()
            (text_dir / f"page_{i:03d}.txt").write_text(text)
            if len(text) >= _MIN_EXTRACTABLE_CHARS:
                n_good_pages += 1
    finally:
        doc.close()

    text_quality = (n_good_pages / n_pages) if n_pages else 0.0

    return schemas.PaperStatus(
        study=study,
        status="found_local",
        pdf_path=f"materials/{study}/paper.pdf",
        sha256=sha256,
        retrieved_from=retrieved_from,
        candidates_tried=[],
        n_pages=n_pages,
        text_quality=text_quality,
        needs_direct_pdf_read=text_quality < 0.5,
        notes=[],
    )
