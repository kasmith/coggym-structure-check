"""Central path configuration.

Why a single module: every stage of the pipeline needs to agree on where the
datasets repo, run artifacts, and downloaded materials live. Centralizing the
logic here (rather than re-deriving paths ad hoc in each stage module) keeps
the path contract enforceable and makes it trivial to override the datasets
repo location in CI or for local testing via an environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Return the root of this repo (coggym-structure-check), derived from this file's location.

    Using ``__file__`` instead of the current working directory means every
    caller gets the same answer regardless of where the CLI was invoked from.
    """
    return Path(__file__).resolve().parents[2]


def datasets_repo() -> Path:
    """Return the path to the cog-gym-datasets clone.

    Resolution order: ``COGGYM_DATASETS_REPO`` env var if set, else
    ``../cog-gym-datasets`` relative to this repo's root. Callers must never
    modify this repo's working tree, index, or HEAD directly (see CLAUDE.md);
    all writes go through ``fixer.py`` via a git worktree.
    """
    override = os.environ.get("COGGYM_DATASETS_REPO")
    if override:
        return Path(override).resolve()
    return repo_root().parent / "cog-gym-datasets"


def runs_dir() -> Path:
    """Return the directory holding per-study run artifacts (gitignored)."""
    return repo_root() / "runs"


def materials_dir() -> Path:
    """Return the directory holding downloaded author materials (gitignored)."""
    return repo_root() / "materials"
