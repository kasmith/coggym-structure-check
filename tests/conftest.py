"""Shared fixtures for tests that need a throwaway `datasets_repo`-shaped git
repository (currently just tests/test_fixer.py, but kept here rather than
test-local since any future task exercising `fixer.py`/`git worktree`
behavior against a fake datasets repo would need the exact same shape).

Why this mirrors the real clone so specifically (detached HEAD + an
untracked file): constraints.md hard rule #3 says the real `cog-gym-datasets`
clone is *always* in this state, and `fixer.py` is the one module allowed to
write into it — every test exercising it must reproduce that state closely
enough to catch a regression that, say, accidentally ran `git checkout` on
the main working tree (which would either fail loudly against a detached
HEAD with local changes, or silently corrupt the untracked file).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, NamedTuple

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


class FakeDatasetsRepo(NamedTuple):
    """One throwaway datasets-repo-shaped git repository."""

    root: Path
    head_sha: str


def _make_fake_datasets_repo(dest: Path) -> FakeDatasetsRepo:
    shutil.copytree(FIXTURES / "studies", dest / "studies")
    shutil.copy(FIXTURES / "papers_represented.csv", dest / "papers_represented.csv")

    _git(dest, "init", "-q")
    _git(dest, "config", "user.email", "test@example.org")
    _git(dest, "config", "user.name", "Test")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "fixture commit")
    sha = _git(dest, "rev-parse", "HEAD").strip()
    _git(dest, "checkout", "-q", "--detach", sha)

    # Mirrors the real clone's untracked scratch files (constraints.md hard
    # rule #3) -- tests assert this is byte-for-byte untouched after
    # fixer.py runs against `dest`.
    (dest / "_scratch_untracked.txt").write_text("mirrors real clone's untracked scaffolding\n")

    return FakeDatasetsRepo(root=dest, head_sha=sha)


@pytest.fixture()
def datasets_repo_factory(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[[], FakeDatasetsRepo]:
    """Factory fixture: each call builds a *new* throwaway datasets repo.

    A factory (not a single fixture value) because some tests need two
    independent repos, or need to inspect repo state before/after in ways
    that are clearer when the construction call is inline in the test body.
    """

    def _factory() -> FakeDatasetsRepo:
        dest = tmp_path_factory.mktemp("fake_datasets_repo")
        return _make_fake_datasets_repo(dest)

    return _factory
