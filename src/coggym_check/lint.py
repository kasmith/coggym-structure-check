"""Deterministic structural lint over one CogGym study's dataset files.

No LLM calls, no network — every check here is a pure function of files
already on disk (or in a pinned commit), reusing Task 3's `StudyReader`
abstraction (`dataset._reader_for`, `FsReader`/`GitReader`) rather than
re-implementing file access.

Why lint.py cannot simply call `dataset.load_study` and re-derive findings
from its `StudySnapshot`: `load_study` *raises* on a config.json/trial.jsonl/
instruction.jsonl that fails to parse for an experiment that is otherwise
present (by design — see its docstring), which would abort linting the
entire study on one bad experiment. Lint's job is the opposite: report the
parse failure as a `json-parse` finding for that one experiment and keep
checking every other experiment. So `_lint_experiment` below does its own
tolerant per-file parse (via `_parse_json_file`/`_parse_jsonl_file`), reusing
only the low-level `StudyReader` + `dataset.CANONICAL_EXPERIMENT_FILES`, and
skips exactly the checks that need a file that failed to parse for that one
experiment (each such skip is annotated at its call site below).
"""

from __future__ import annotations

import json
import re

from coggym_check import config, dataset
from coggym_check.dataset import CANONICAL_EXPERIMENT_FILES, StudyReader
from coggym_check.schemas import LintFinding, LintReport

#: Every check_id this module can emit, in a fixed declarative order. This is
#: exactly what `LintReport.checks_run` reports: lint always *attempts* every
#: check in this list for every study (regardless of whether any particular
#: experiment's parse failure means that check produces no findings for that
#: one experiment) -- `checks_run` is not "checks that found something".
ALL_CHECK_IDS = [
    "files-present",
    "json-parse",
    "flow-ids-resolve",
    "ids-referenced",
    "randomization-length",
    "stimuli-count",
    "response-type",
    "assets-exist",
    "slider-sane",
    "human-data-ids",
    "citation-present",
    "doi-shape",
    "extra-files",
    "non-experiment-dirs",
]

#: Citation fields lint expects a well-formed experiment config to carry
#: (constraints.md: present in 259/265 real experiments).
_CITATION_FIELDS = ("paper-title", "citation", "authors", "year")

#: `human_data_mean.json`/`human_data_ind.json` header keys that are not
#: trial ids and must be ignored by `human-data-ids` (constraints.md's
#: dataset schema cheat-sheet).
_HUMAN_DATA_HEADER_KEYS = {"participants_info", "judgment_count"}

#: A bare DOI like `10.1000/test` (constraints.md: paperDOI may be a bare DOI
#: string, per Baker2017Rational/exp1's `10.1000/test`-shaped anchor value).
_DOI_BARE_RE = re.compile(r"^10\.\d{4,9}/\S+$")

#: A `doi.org` URL wrapping a DOI, e.g. `https://doi.org/10.1000/test`.
_DOI_URL_RE = re.compile(r"^https?://(dx\.)?doi\.org/10\.\d{4,9}/\S+$")


def lint_study(study: str, commit: str | None = None) -> LintReport:
    """Run every structural check over `studies/<study>/` and return a `LintReport`.

    Mirrors `dataset.load_study`'s study-not-found behavior (raises
    `dataset.StudyNotFoundError`) and its reader selection (filesystem vs. a
    pinned commit via `--commit`), but never lets one experiment's malformed
    file abort the rest of the study -- see the module docstring.
    """
    reader = dataset._reader_for(commit)
    study_dir = f"studies/{study}"
    if not reader.exists(study_dir):
        raise dataset.StudyNotFoundError(
            f"study '{study}' not found under '{study_dir}' "
            f"(commit={commit!r}, datasets_repo={config.datasets_repo()})"
        )

    entries = reader.list_entries(study_dir)
    exp_names = sorted(
        e.name for e in entries if e.is_dir and reader.exists(f"{study_dir}/{e.name}/config.json")
    )
    non_experiment_dirs = sorted(
        e.name for e in entries if e.is_dir and e.name not in exp_names
    )

    findings: list[LintFinding] = []
    for name in non_experiment_dirs:
        findings.append(
            LintFinding(
                check_id="non-experiment-dirs",
                level="info",
                experiment=None,
                message=(
                    f"'{name}' has no config.json; skipped as a non-experiment "
                    "directory (e.g. Baker2017Rational/exp2 holds only "
                    "symbolic_representation/, no canonical files at all)"
                ),
                location=f"{study_dir}/{name}",
            )
        )

    fs_mode = commit is None
    for exp_name in exp_names:
        findings.extend(_lint_experiment(reader, study_dir, exp_name, fs_mode))

    return LintReport(study=study, checks_run=list(ALL_CHECK_IDS), findings=findings)


def _lint_experiment(
    reader: StudyReader, study_dir: str, exp_name: str, fs_mode: bool
) -> list[LintFinding]:
    """Run every per-experiment check, tolerating any single file's parse failure.

    Each of the five per-experiment files is parsed independently (into
    `None` on failure, with a `json-parse` finding recorded); every later
    check that needs one of those files is skipped -- not crashed -- when
    its input is `None`. `files-present` and `extra-files` need no parsed
    data at all, so they always run.
    """
    exp_dir = f"{study_dir}/{exp_name}"
    findings: list[LintFinding] = []

    for fname in sorted(CANONICAL_EXPERIMENT_FILES):
        if not reader.exists(f"{exp_dir}/{fname}"):
            findings.append(
                LintFinding(
                    check_id="files-present",
                    level="error",
                    experiment=exp_name,
                    message=f"missing canonical file '{fname}'",
                    location=f"{exp_dir}/{fname}",
                )
            )

    config_data = _parse_json_file(reader, f"{exp_dir}/config.json", exp_name, findings)
    trial_records = _parse_jsonl_file(reader, f"{exp_dir}/trial.jsonl", exp_name, findings)
    instruction_records = _parse_jsonl_file(
        reader, f"{exp_dir}/instruction.jsonl", exp_name, findings
    )
    human_mean = _parse_json_file(
        reader, f"{exp_dir}/human_data_mean.json", exp_name, findings
    )
    human_ind = _parse_json_file(reader, f"{exp_dir}/human_data_ind.json", exp_name, findings)

    trial_ids = (
        {r["id"] for r in trial_records if "id" in r} if trial_records is not None else None
    )
    instruction_ids = (
        {r["id"] for r in instruction_records if "id" in r}
        if instruction_records is not None
        else None
    )

    if config_data is not None and trial_ids is not None and instruction_ids is not None:
        _check_flow_ids_resolve(config_data, trial_ids, instruction_ids, exp_name, findings)
        _check_ids_referenced(config_data, trial_ids, instruction_ids, exp_name, findings)

    if config_data is not None:
        _check_randomization_length(config_data, exp_name, findings)
        _check_citation_present(config_data, exp_name, findings)
        _check_doi_shape(config_data, exp_name, findings)

    if config_data is not None and trial_records is not None:
        _check_stimuli_count(config_data, trial_records, exp_name, findings)
        _check_response_type(config_data, trial_records, exp_name, findings)

    if trial_records is not None:
        _check_assets_exist(reader, exp_dir, trial_records, exp_name, fs_mode, findings)
        _check_slider_sane(trial_records, exp_name, findings)

    if trial_ids is not None:
        if human_mean is not None:
            _check_human_data_ids(
                human_mean, trial_ids, exp_name, "human_data_mean.json", findings
            )
        if human_ind is not None:
            _check_human_data_ids(
                human_ind, trial_ids, exp_name, "human_data_ind.json", findings
            )

    _check_extra_files(reader, exp_dir, exp_name, findings)

    return findings


# ---------------------------------------------------------------------------
# Tolerant per-file parsing (json-parse check)
# ---------------------------------------------------------------------------


def _parse_json_file(
    reader: StudyReader, relpath: str, exp_name: str, findings: list[LintFinding]
) -> dict | None:
    """Read+parse one JSON file, recording a `json-parse` finding on failure.

    Returns `None` both when the file is absent (that's `files-present`'s
    job to report, not this check's) and when it fails to parse -- callers
    treat both cases identically: skip whatever check needed this data.
    """
    if not reader.exists(relpath):
        return None
    try:
        text = reader.read_text(relpath)
    except FileNotFoundError:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        findings.append(
            LintFinding(
                check_id="json-parse",
                level="error",
                experiment=exp_name,
                message=f"{relpath} is not valid JSON: {exc}",
                location=relpath,
            )
        )
        return None


def _parse_jsonl_file(
    reader: StudyReader, relpath: str, exp_name: str, findings: list[LintFinding]
) -> list[dict] | None:
    """Read+parse one JSONL file line by line, recording a `json-parse`
    finding (naming the failing line) and returning `None` on the first
    unparseable line -- one finding per file is enough to flag the problem;
    exhaustively reporting every subsequent line would just be noise."""
    if not reader.exists(relpath):
        return None
    try:
        text = reader.read_text(relpath)
    except FileNotFoundError:
        return None
    records: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            findings.append(
                LintFinding(
                    check_id="json-parse",
                    level="error",
                    experiment=exp_name,
                    message=f"{relpath} line {lineno} is not valid JSON: {exc}",
                    location=f"{relpath}:{lineno}",
                )
            )
            return None
    return records


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_flow_ids_resolve(
    config_data: dict,
    trial_ids: set[str],
    instruction_ids: set[str],
    exp_name: str,
    findings: list[LintFinding],
) -> None:
    """Every id in `experimentFlow[*].blocks` must resolve to a trial.jsonl
    or instruction.jsonl id (error: a dangling id would crash the real
    experiment runner at that block)."""
    valid_ids = trial_ids | instruction_ids
    for cond_idx, cond in enumerate(config_data.get("experimentFlow", [])):
        cond_name = cond.get("experimental_condition", "?")
        for block_idx, block in enumerate(cond.get("blocks", [])):
            for id_ in block:
                if id_ not in valid_ids:
                    findings.append(
                        LintFinding(
                            check_id="flow-ids-resolve",
                            level="error",
                            experiment=exp_name,
                            message=(
                                f"id '{id_}' in condition '{cond_name}' block {block_idx} "
                                "does not resolve to any trial.jsonl/instruction.jsonl id"
                            ),
                            location=f"experimentFlow[{cond_idx}].blocks[{block_idx}]",
                        )
                    )


def _check_ids_referenced(
    config_data: dict,
    trial_ids: set[str],
    instruction_ids: set[str],
    exp_name: str,
    findings: list[LintFinding],
) -> None:
    """Every trial/instruction id should be referenced by >=1 condition's
    blocks (warning, not error: an unused trial doesn't break anything, but
    likely means dead/leftover data)."""
    referenced: set[str] = set()
    for cond in config_data.get("experimentFlow", []):
        for block in cond.get("blocks", []):
            referenced.update(block)
    for id_ in sorted(trial_ids | instruction_ids):
        if id_ not in referenced:
            kind = "trial" if id_ in trial_ids else "instruction"
            findings.append(
                LintFinding(
                    check_id="ids-referenced",
                    level="warning",
                    experiment=exp_name,
                    message=f"{kind} id '{id_}' is not referenced by any condition's blocks",
                    location=id_,
                )
            )


def _check_randomization_length(
    config_data: dict, exp_name: str, findings: list[LintFinding]
) -> None:
    """When a condition's `block_randomization` is present, its length must
    equal its `blocks` length (error: `block_randomization` is positional,
    per-block; a length mismatch means at least one block's flag is
    meaningless/misapplied). `block_randomization` is optional (constraints.md:
    absent e.g. in Gerstenberg2015How/exp1) -- absence is not a finding."""
    for cond_idx, cond in enumerate(config_data.get("experimentFlow", [])):
        randomization = cond.get("block_randomization")
        if randomization is None:
            continue
        n_blocks = len(cond.get("blocks", []))
        if len(randomization) != n_blocks:
            findings.append(
                LintFinding(
                    check_id="randomization-length",
                    level="error",
                    experiment=exp_name,
                    message=(
                        f"condition '{cond.get('experimental_condition', '?')}' "
                        f"block_randomization has {len(randomization)} entries but "
                        f"there are {n_blocks} blocks"
                    ),
                    location=f"experimentFlow[{cond_idx}].block_randomization",
                )
            )


def _check_stimuli_count(
    config_data: dict, trial_records: list[dict], exp_name: str, findings: list[LintFinding]
) -> None:
    """`stimuli_count` should equal the number of entries in trial.jsonl.

    Interpretation choice (pre-answered clarification): both real anchors in
    constraints.md tie `stimuli_count` to trial *count*, not distinct-stimuli
    count -- Baker2017Rational/exp1 has `stimuli_count` 156 for 156 trials,
    and Gerstenberg2015How/exp1 has `stimuli_count` 64 for 64 trials. Warning
    level: a mismatch is metadata drift, not a broken experiment.
    """
    stimuli_count = config_data.get("stimuli_count")
    if stimuli_count is None:
        return
    n_trials = len(trial_records)
    if stimuli_count != n_trials:
        findings.append(
            LintFinding(
                check_id="stimuli-count",
                level="warning",
                experiment=exp_name,
                message=(
                    f"config stimuli_count ({stimuli_count}) does not match "
                    f"trial.jsonl entry count ({n_trials})"
                ),
                location="config.json:stimuli_count",
            )
        )


def _check_response_type(
    config_data: dict, trial_records: list[dict], exp_name: str, findings: list[LintFinding]
) -> None:
    """Config `responseType` should equal the set of query types actually
    used across trial.jsonl, ignoring `text-instruction` (a query type used
    for inline instructional text, not a real response modality)."""
    declared = set(config_data.get("responseType", []))
    actual = {
        query.get("type")
        for record in trial_records
        for query in record.get("queries", [])
        if query.get("type") not in (None, "text-instruction")
    }
    if declared != actual:
        findings.append(
            LintFinding(
                check_id="response-type",
                level="warning",
                experiment=exp_name,
                message=(
                    f"config responseType {sorted(declared)} does not match "
                    f"query types actually used {sorted(actual)}"
                ),
                location="config.json:responseType",
            )
        )


def _check_assets_exist(
    reader: StudyReader,
    exp_dir: str,
    trial_records: list[dict],
    exp_name: str,
    fs_mode: bool,
    findings: list[LintFinding],
) -> None:
    """Every relative (non-http) `media_url` entry must resolve under the
    experiment folder (error). fs mode only (pre-answered clarification):
    `GitReader.exists` can check a *path*'s presence at the pinned commit
    just as well, but confirming binary media assets round-trip correctly
    via `git show` is out of scope for a structural lint pass -- commit mode
    instead emits one info finding per experiment saying assets weren't
    checked."""
    if not fs_mode:
        findings.append(
            LintFinding(
                check_id="assets-exist",
                level="info",
                experiment=exp_name,
                message="assets were not checked (commit mode)",
                location=None,
            )
        )
        return
    for record in trial_records:
        trial_id = record.get("id", "?")
        for stim in record.get("stimuli", []):
            for url in stim.get("media_url") or []:
                if url.startswith("http://") or url.startswith("https://"):
                    continue
                relpath = f"{exp_dir}/{url}"
                if not reader.exists(relpath):
                    findings.append(
                        LintFinding(
                            check_id="assets-exist",
                            level="error",
                            experiment=exp_name,
                            message=(
                                f"trial '{trial_id}' references media_url '{url}' "
                                "which does not exist under the experiment folder"
                            ),
                            location=relpath,
                        )
                    )


def _check_slider_sane(
    trial_records: list[dict], exp_name: str, findings: list[LintFinding]
) -> None:
    """Every slider query's `min` must be < `max` (error), and every labeled
    value must fall within `[min, max]` (error) -- read directly off each
    trial's own `slider_config` (rather than the aggregated
    `queries_by_tag` summary) so the finding can name the exact trial."""
    for record in trial_records:
        trial_id = record.get("id", "?")
        for query in record.get("queries", []):
            slider_config = query.get("slider_config")
            if slider_config is None:
                continue
            tag = query.get("tag", "?")
            min_v = slider_config.get("min")
            max_v = slider_config.get("max")
            if not isinstance(min_v, (int, float)) or not isinstance(max_v, (int, float)):
                continue
            if min_v >= max_v:
                findings.append(
                    LintFinding(
                        check_id="slider-sane",
                        level="error",
                        experiment=exp_name,
                        message=(
                            f"trial '{trial_id}' query '{tag}' slider_config min "
                            f"({min_v}) is not less than max ({max_v})"
                        ),
                        location=f"trial.jsonl:{trial_id}:{tag}",
                    )
                )
                continue
            for label in slider_config.get("labels", []):
                value = label.get("value")
                if isinstance(value, (int, float)) and not (min_v <= value <= max_v):
                    findings.append(
                        LintFinding(
                            check_id="slider-sane",
                            level="error",
                            experiment=exp_name,
                            message=(
                                f"trial '{trial_id}' query '{tag}' slider label value "
                                f"{value} is outside [{min_v}, {max_v}]"
                            ),
                            location=f"trial.jsonl:{trial_id}:{tag}",
                        )
                    )


def _check_human_data_ids(
    human_data: dict,
    trial_ids: set[str],
    exp_name: str,
    filename: str,
    findings: list[LintFinding],
) -> None:
    """Every non-header key in a human_data file must be a trial.jsonl id
    (warning: an orphan key usually means stale data from a renamed/removed
    trial)."""
    for key in human_data:
        if key in _HUMAN_DATA_HEADER_KEYS:
            continue
        if key not in trial_ids:
            findings.append(
                LintFinding(
                    check_id="human-data-ids",
                    level="warning",
                    experiment=exp_name,
                    message=f"{filename} has key '{key}' which is not a trial id in trial.jsonl",
                    location=f"{filename}:{key}",
                )
            )


def _check_citation_present(
    config_data: dict, exp_name: str, findings: list[LintFinding]
) -> None:
    """Citation fields (`paper-title`, `citation`, `authors`, `year`) should
    all be present and non-empty (warning: constraints.md notes 6/265 real
    configs lack citation info entirely -- common enough to be a warning,
    not an error)."""
    missing = [f for f in _CITATION_FIELDS if not config_data.get(f)]
    if missing:
        findings.append(
            LintFinding(
                check_id="citation-present",
                level="warning",
                experiment=exp_name,
                message=f"missing/empty citation field(s): {', '.join(missing)}",
                location="config.json",
            )
        )


def _check_doi_shape(config_data: dict, exp_name: str, findings: list[LintFinding]) -> None:
    """`paperDOI` should be empty, a bare DOI (`10.xxxx/...`), or a
    `doi.org` URL wrapping one (info: constraints.md notes paperDOI "may be
    empty or a non-DOI URL" in the real data -- e.g. a plain webpage link --
    which is worth flagging but never breaks anything, hence info not
    warning/error)."""
    doi = config_data.get("paperDOI")
    if not doi:
        findings.append(
            LintFinding(
                check_id="doi-shape",
                level="info",
                experiment=exp_name,
                message="paperDOI is empty",
                location="config.json:paperDOI",
            )
        )
        return
    if not (_DOI_BARE_RE.match(doi) or _DOI_URL_RE.match(doi)):
        findings.append(
            LintFinding(
                check_id="doi-shape",
                level="info",
                experiment=exp_name,
                message=f"paperDOI '{doi}' does not look like a DOI or doi.org URL",
                location="config.json:paperDOI",
            )
        )


def _check_extra_files(
    reader: StudyReader, exp_dir: str, exp_name: str, findings: list[LintFinding]
) -> None:
    """List any experiment-folder entry that isn't one of the six canonical
    files, `assets/`, `paper.pdf`, or a `ground_truth_*` file (info, never an
    error -- constraints.md: "treat unknown extras as info, not errors")."""
    entries = reader.list_entries(exp_dir)
    extras = sorted(
        e.name
        for e in entries
        if e.name not in CANONICAL_EXPERIMENT_FILES
        and e.name != "assets"
        and e.name != "paper.pdf"
        and not e.name.startswith("ground_truth_")
    )
    if extras:
        findings.append(
            LintFinding(
                check_id="extra-files",
                level="info",
                experiment=exp_name,
                message=f"unrecognized extra file(s)/dir(s): {', '.join(extras)}",
                location=exp_dir,
            )
        )
