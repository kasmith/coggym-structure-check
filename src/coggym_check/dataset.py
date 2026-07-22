"""Deterministic parsing of one CogGym study into a validated `StudySnapshot`.

Why a `StudyReader` abstraction: every parsing function below needs to read
files and list directories, but the same logic must work against two very
different backends — the datasets repo's live working tree (filesystem
mode) and a single pinned commit read via `git show`/`git ls-tree` without
ever checking it out (constraints.md hard rule #3: the datasets repo clone
is on a detached HEAD and its working tree/index/HEAD must never be
touched). `FsReader` and `GitReader` implement the same small `StudyReader`
protocol so `load_study` (and Task 4's `lint.py`, which reuses this module)
never has to branch on which mode it's in.

Deliberately NOT read anywhere in this module: `trial_selection_map_100.json`
(constraints.md hard rule #4 — it is AI-testing scaffolding, out of scope).
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from coggym_check import config
from coggym_check.schemas import (
    Citation,
    ConditionSnapshot,
    ExperimentSnapshot,
    HumanDataSummary,
    QuerySummary,
    SliderLabel,
    SliderSummary,
    StudySnapshot,
)
from coggym_check import __version__

#: The six files every experiment folder is expected to have (constraints.md's
#: dataset schema cheat-sheet). Anything else directly under an experiment
#: folder — other than `assets/`, `ground_truth_*`, and `paper.pdf` — counts
#: as an `extra_files` entry (info, never an error; see lint.py for that
#: judgment).
CANONICAL_EXPERIMENT_FILES = {
    "config.json",
    "trial.jsonl",
    "instruction.jsonl",
    "human_data_mean.json",
    "human_data_ind.json",
    "README.md",
}


class StudyNotFoundError(RuntimeError):
    """Raised when `studies/<study>/` doesn't exist under the reader's root.

    A dedicated exception (rather than a bare FileNotFoundError) so callers
    — the CLI in particular — can print a clean, study-specific message
    instead of a raw path traceback.
    """


@dataclass(frozen=True)
class DirEntry:
    """One name+kind pair returned by `StudyReader.list_entries`."""

    name: str
    is_dir: bool


class StudyReader(Protocol):
    """Minimal file-access abstraction shared by dataset parsing and lint.

    Three primitives are all either consumer needs: does this path exist,
    read a file's full text, and list one directory's immediate children
    (non-recursive — every caller that needs to go deeper calls again with
    the child path). Kept intentionally small so both implementations stay
    easy to reason about and so a future backend (e.g. a tarball reader)
    only has to implement three methods.
    """

    def exists(self, relpath: str) -> bool:
        """Return whether `relpath` (posix-style, relative to the reader's root) exists."""
        ...

    def read_text(self, relpath: str) -> str:
        """Return the full text contents of `relpath`. Raises FileNotFoundError if absent."""
        ...

    def list_entries(self, relpath: str) -> list[DirEntry]:
        """Return the immediate children of the directory at `relpath`, sorted by name.

        Returns an empty list if `relpath` doesn't exist or isn't a directory
        (callers that need to distinguish "missing" from "empty" should
        check `exists` first).
        """
        ...


class FsReader:
    """Reads a live working tree — the datasets repo's checked-out files."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def exists(self, relpath: str) -> bool:
        return (self.root / relpath).exists()

    def read_text(self, relpath: str) -> str:
        path = self.root / relpath
        try:
            return path.read_text()
        except OSError as exc:
            raise FileNotFoundError(f"{relpath}: {exc}") from exc

    def list_entries(self, relpath: str) -> list[DirEntry]:
        dirpath = self.root / relpath
        if not dirpath.is_dir():
            return []
        entries = [DirEntry(name=p.name, is_dir=p.is_dir()) for p in dirpath.iterdir()]
        return sorted(entries, key=lambda e: e.name)


class GitReader:
    """Reads file contents at one pinned commit via `git show`/`git ls-tree`.

    Never runs `git checkout` (or anything else that would mutate the
    repo's working tree, index, or HEAD) — every operation here is a
    read-only query against a specific commit's tree object, safe to run
    against the real datasets repo's detached-HEAD clone.
    """

    def __init__(self, repo: Path, commit: str) -> None:
        self.repo = repo
        self.commit = commit

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
        )

    def exists(self, relpath: str) -> bool:
        result = self._run("cat-file", "-e", f"{self.commit}:{relpath}")
        return result.returncode == 0

    def read_text(self, relpath: str) -> str:
        result = self._run("show", f"{self.commit}:{relpath}")
        if result.returncode != 0:
            raise FileNotFoundError(f"{relpath}@{self.commit}: {result.stderr.strip()}")
        return result.stdout

    def list_entries(self, relpath: str) -> list[DirEntry]:
        treeish = f"{self.commit}:{relpath}" if relpath else self.commit
        result = self._run("ls-tree", treeish)
        if result.returncode != 0:
            return []
        entries = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            meta, name = line.split("\t", 1)
            _mode, kind, _sha = meta.split()
            entries.append(DirEntry(name=name, is_dir=(kind == "tree")))
        return sorted(entries, key=lambda e: e.name)


def _now_iso() -> str:
    """ISO-8601 UTC timestamp, e.g. `2026-07-22T00:00:00Z` (pre-answered
    clarification: produced fresh at snapshot time)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reader_for(commit: str | None, repo_root: Path | None = None) -> StudyReader:
    """Build the reader for `commit` (fs mode if `None`, else `GitReader`).

    `repo_root` (task-8 addition): override the datasets-repo root instead of
    `config.datasets_repo()`. Needed so `fixer.py` can re-run `lint_study`
    (via this function) against a `git worktree` checkout after applying
    fixes, without ever touching the real datasets repo's working tree —
    that worktree lives at an arbitrary `runs/<study>/.worktree` path, not
    at `config.datasets_repo()`.
    """
    repo = repo_root if repo_root is not None else config.datasets_repo()
    if commit is not None:
        return GitReader(repo, commit)
    return FsReader(repo)


def load_study(study: str, commit: str | None = None) -> StudySnapshot:
    """Parse `studies/<study>/` (filesystem, or a pinned commit if given) into
    a validated `StudySnapshot`.

    Tolerates: missing citation fields per experiment config, an absent
    `block_randomization` key, and malformed/missing human-data header
    values (all become `None` rather than raising — lint.py is where those
    become visible findings). Does NOT tolerate a missing study folder
    (raises `StudyNotFoundError`) or missing/unparseable
    config.json/trial.jsonl/instruction.jsonl for an experiment that *is*
    present — those are basic file-presence/parse problems surfaced as-is
    here and re-checked deterministically by lint.py's `files-present` and
    `json-parse` checks.

    A subdirectory of `studies/<study>/` only counts as an experiment if it
    contains a `config.json`. Real-data finding: `Baker2017Rational/exp2`
    is a bare directory holding only `symbolic_representation/` (no
    canonical files at all -- presumably stray auxiliary data), the only
    such case across all 100 real studies. Without this filter that
    directory would crash the parse on a missing config.json/trial.jsonl.
    """
    reader = _reader_for(commit)
    study_dir = f"studies/{study}"
    if not reader.exists(study_dir):
        raise StudyNotFoundError(
            f"study '{study}' not found under '{study_dir}' "
            f"(commit={commit!r}, datasets_repo={config.datasets_repo()})"
        )

    exp_names = sorted(
        entry.name
        for entry in reader.list_entries(study_dir)
        if entry.is_dir and reader.exists(f"{study_dir}/{entry.name}/config.json")
    )
    experiments = {name: _load_experiment(reader, study_dir, name) for name in exp_names}

    return StudySnapshot(
        study=study,
        commit=commit,
        generated_at=_now_iso(),
        tool_version=__version__,
        paper_pdf_present=reader.exists(f"{study_dir}/paper.pdf"),
        source_url_csv=_lookup_source_url(reader, study),
        citation=_load_citation(reader, study_dir, exp_names),
        experiments=experiments,
    )


def _load_citation(reader: StudyReader, study_dir: str, exp_names: list[str]) -> Citation:
    """Build the study-level `Citation` from the first experiment's config.

    Citation fields (paper-title, citation, paperDOI, authors, year) live
    per-experiment-config in the real data but describe the same paper for
    every experiment in a study; taking the first experiment (sorted, so
    deterministic) is a reasonable single source rather than requiring all
    experiments to agree. Missing fields become `None`/empty list — 6/265
    real configs lack citation info entirely (constraints.md), and this
    must still produce a valid all-`None` `Citation`.
    """
    if not exp_names:
        return Citation()
    config_data = _read_json_tolerant(reader, f"{study_dir}/{exp_names[0]}/config.json")
    authors = config_data.get("authors")
    year = config_data.get("year")
    return Citation(
        paper_title=config_data.get("paper-title") or None,
        citation=config_data.get("citation") or None,
        paperDOI=config_data.get("paperDOI") or None,
        authors=list(authors) if isinstance(authors, list) else [],
        year=year if isinstance(year, int) else None,
    )


def _load_experiment(reader: StudyReader, study_dir: str, exp_name: str) -> ExperimentSnapshot:
    exp_dir = f"{study_dir}/{exp_name}"
    config_data = _read_json(reader, f"{exp_dir}/config.json")
    trial_records = _read_jsonl(reader, f"{exp_dir}/trial.jsonl")
    instruction_records = _read_jsonl(reader, f"{exp_dir}/instruction.jsonl")

    trial_ids = {r["id"] for r in trial_records if "id" in r}
    instruction_ids = {r["id"] for r in instruction_records if "id" in r}

    conditions = [
        _build_condition(cond, trial_ids, instruction_ids)
        for cond in config_data.get("experimentFlow", [])
    ]

    entries = reader.list_entries(exp_dir)
    entry_names = {e.name for e in entries}
    assets_present = any(e.name == "assets" and e.is_dir for e in entries)
    extra_files = sorted(
        name
        for name in entry_names
        if name not in CANONICAL_EXPERIMENT_FILES
        and name != "assets"
        and name != "paper.pdf"
        and not name.startswith("ground_truth_")
    )

    human_mean = _read_json_tolerant(reader, f"{exp_dir}/human_data_mean.json")

    return ExperimentSnapshot(
        experiment_name=config_data.get("experimentName", exp_name),
        description=config_data.get("description", ""),
        task_type=list(config_data.get("taskType", [])),
        response_type=list(config_data.get("responseType", [])),
        stimuli_count=config_data.get("stimuli_count"),
        n_trials_in_jsonl=len(trial_records),
        n_instructions=len(instruction_records),
        conditions=conditions,
        queries_by_tag=_aggregate_queries(trial_records),
        instruction_texts={
            r["id"]: r.get("text", "") for r in instruction_records if "id" in r
        },
        stimuli_modalities=_stimuli_modalities(trial_records),
        assets_present=assets_present,
        extra_files=extra_files,
        human_data=HumanDataSummary(
            participants_count=_safe_get_int(human_mean, ("participants_info", "count")),
            judgment_count=_safe_get_int(human_mean, ("judgment_count",)),
        ),
    )


def _build_condition(
    cond: dict, trial_ids: set[str], instruction_ids: set[str]
) -> ConditionSnapshot:
    blocks: list[list[str]] = cond.get("blocks", [])
    raw_randomization = cond.get("block_randomization")
    block_randomization = list(raw_randomization) if raw_randomization is not None else None
    return ConditionSnapshot(
        name=cond.get("experimental_condition", ""),
        n_blocks=len(blocks),
        block_sizes=[len(block) for block in blocks],
        block_randomization=block_randomization,
        block_kinds=[_classify_block(block, trial_ids, instruction_ids) for block in blocks],
    )


def _classify_block(
    ids: list[str], trial_ids: set[str], instruction_ids: set[str]
) -> str:
    """Classify one block by where its ids resolve.

    All ids found in instruction.jsonl -> "instruction"; all found in
    trial.jsonl -> "trial"; otherwise "mixed" (this also covers ids that
    resolve nowhere at all — an unresolvable id makes the block "mixed"
    rather than crashing the snapshot; lint.py's `flow-ids-resolve` check is
    where that becomes a visible finding). An empty block is classified
    "instruction" (vacuously a subset of both sets; picked over "trial"
    only because instruction blocks come first in practice — this is an
    edge case the real data doesn't hit).
    """
    id_set = set(ids)
    if id_set <= instruction_ids:
        return "instruction"
    if id_set <= trial_ids:
        return "trial"
    return "mixed"


def _aggregate_queries(trial_records: list[dict]) -> dict[str, QuerySummary]:
    """Aggregate every query tag seen across an experiment's trials.

    `options`/`slider` are filled in from the first trial that carries a
    given tag with that data (they're expected to be identical across
    trials sharing a tag); `n_trials_using` counts every trial record whose
    queries include the tag.
    """
    agg: dict[str, dict] = {}
    for record in trial_records:
        for query in record.get("queries", []):
            tag = query.get("tag")
            if tag is None:
                continue
            entry = agg.setdefault(
                tag,
                {"type": query.get("type", ""), "options": None, "slider": None, "n_trials_using": 0},
            )
            entry["n_trials_using"] += 1
            option = query.get("option")
            if entry["options"] is None and option is not None:
                entry["options"] = list(option)
            slider_config = query.get("slider_config")
            if entry["slider"] is None and slider_config is not None:
                entry["slider"] = SliderSummary(
                    min=slider_config.get("min"),
                    max=slider_config.get("max"),
                    labels=[
                        SliderLabel(value=label.get("value"), label=label.get("label", ""))
                        for label in slider_config.get("labels", [])
                    ],
                )
    return {tag: QuerySummary(**data) for tag, data in agg.items()}


def _stimuli_modalities(trial_records: list[dict]) -> list[str]:
    modalities = {
        stim.get("input_type")
        for record in trial_records
        for stim in record.get("stimuli", [])
        if stim.get("input_type") is not None
    }
    return sorted(modalities)


def _lookup_source_url(reader: StudyReader, study: str) -> str | None:
    """Match `study` against the root `papers_represented.csv`'s
    semicolon-separated `experiments` column (each entry `study/expN`), and
    return that row's `source_url` (or `None` if unmatched/blank — 99/100
    rows have it populated, per constraints.md)."""
    if not reader.exists("papers_represented.csv"):
        return None
    text = reader.read_text("papers_represented.csv")
    for row in csv.DictReader(io.StringIO(text)):
        experiments = row.get("experiments", "") or ""
        folders = {part.strip().split("/")[0] for part in experiments.split(";") if part.strip()}
        if study in folders:
            url = (row.get("source_url") or "").strip()
            return url or None
    return None


def _read_json(reader: StudyReader, relpath: str) -> dict:
    return json.loads(reader.read_text(relpath))


def _read_json_tolerant(reader: StudyReader, relpath: str) -> dict:
    """Read+parse JSON, tolerating an absent file or malformed contents by
    returning `{}` — used exactly where the brief calls for tolerance
    (citation fields, human-data headers)."""
    try:
        return json.loads(reader.read_text(relpath))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _read_jsonl(reader: StudyReader, relpath: str) -> list[dict]:
    records = []
    for line in reader.read_text(relpath).splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def _safe_get_int(data: dict, path: tuple[str, ...]) -> int | None:
    """Walk a dotted path of dict keys, returning the value only if every
    step resolves and the final value is a plain int (not bool). Anything
    else (missing key, wrong type, non-dict intermediate) -> `None`,
    tolerating malformed human-data headers as required."""
    value: object = data
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
