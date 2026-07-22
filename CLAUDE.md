# coggym-structure-check

An agentic system that validates CogGym experiment implementations (in the
`cog-gym-datasets` repo) against the structure described in their source
papers and author-released OSF/GitHub materials. Staged pipeline: each stage
writes one validated JSON/MD artifact to `runs/<study>[@<shortsha>]/`;
deterministic work is plain Python (`src/coggym_check/`); judgment work is
Claude subagents (`.claude/agents/*.md`) orchestrated by a `/check-study`
skill interactively or by `python -m coggym_check run` headlessly.

## Path contract

- Datasets repo lives at `../cog-gym-datasets` (sibling of this repo),
  overridable via env `COGGYM_DATASETS_REPO`.
- The clone is on a **detached HEAD** with untracked files. **NEVER** modify
  its working tree, index, or HEAD directly — no edits, no `git add`/
  `commit`/`checkout`/`reset` there.
- All writes to that repo happen via `python -m coggym_check apply-fixes`,
  which uses a `git worktree` internally. **Never `git push`** from that repo.

## Dataset schema cheat-sheet

- `studies/<Study>/`: study `README.md`, usually `paper.pdf` (95/100),
  experiment folders `exp1`, `exp2`, `exp3a`, ...
- Every experiment folder: `config.json`, `trial.jsonl`, `instruction.jsonl`,
  `human_data_mean.json`, `human_data_ind.json`, `README.md` (+ optional
  `assets/`, `ground_truth_*.json`, other extras — treat unknown extras as
  info, not errors).
- `config.json`: `experimentName`, `description`, `taskType[]`,
  `responseType[]`, `stimuli_count`, `experimentFlow`; citation fields
  `paper-title`, `citation`, `paperDOI` (may be empty/non-DOI URL), `authors`,
  `year` (present in 259/265 experiments).
- `experimentFlow` = list of `{experimental_condition, blocks: [[id, ...],
  ...], block_randomization: [bool, ...]}`; `block_randomization` is
  **optional** (e.g. absent in Gerstenberg2015How/exp1). Block ids reference
  `trial.jsonl` / `instruction.jsonl` ids.
- `trial.jsonl` line: `{id, stimuli: [{input_type: video|image|text,
  media_url: [..]|text: "<html>"}], queries: [{prompt, type, tag,
  slider_config{min,max,labels:[{value,label}],...}, option, required}]}`.
  Query types: `multi-choice`, `single-slider`, `multi-slider`, `textbox`,
  `multi-select`, `text-instruction`.
- `instruction.jsonl` line: `{id, type: "instruction", text: "<html>"}`.
- `human_data_mean.json` / `human_data_ind.json`: `{participants_info:
  {count, gender, age}, judgment_count, <trial_id>: {<tag>: ...}}`.
- Root `papers_represented.csv`: `paper_title, authors, Potential reviewer,
  experiment_count, experiments` (semicolon-separated `study/expN` list),
  `source_url` (99/100 populated).
- Studies missing `paper.pdf`: Baker2017Rational, Chandra2023Inferring,
  Sucholutsky2023Using, Ullman2018Learning, Ying2026Pragmatic.
- Legacy exception: `wu2023a_computational` keeps instruction prose in
  `assets/instructions/text.js`.

`trial_selection_map_100.json` is out of scope — never read it.

## Pipeline stages

| # | Stage | Producer | Artifact |
|---|-------|----------|----------|
| 0 | snapshot + lint | Python | `study_snapshot.json`, `lint.json`, `run_meta.json` |
| 1 | paper acquisition + extraction | Python (+ `paper-finder` agent if `paper.pdf` missing) | `paper_status.json` |
| 2 | materials discovery + download | `materials-scout` agent + Python `download` | `materials_manifest.json` |
| 3 | paper structure synthesis | `paper-analyst` agent (opus) | `paper_summary.json` |
| 4 | materials supplementation | `materials-analyst` agent | `materials_summary.json` |
| 5 | comparison | `structure-comparator` agent (opus) | `comparison.json` |
| 6 | report + prepared PR | `fix-drafter` agent + Python `apply-fixes`/`render` | `fix_plan.json`, `report.md`, `pr.md`, branch `fix/<study>-structure` |

## Fix conservatism (verbatim)

**Fix conservatism (decision 4, verbatim policy):** auto-fix exactly 4
categories: (i) instruction text differing in *substantive content*
(cover-story facts, scale anchors, payoffs, task rules) from *verbatim
materials text* — delivery-medium rewording (lab→online, keypress→slider) is
info-only, never fixed; paper paraphrase is never grounds for a fix; (ii)
`block_randomization` flags contradicting explicit paper/materials evidence;
(iii) slider labels/anchors/range contradicting explicit evidence; (iv)
citation/DOI errors. Everything else = needs-human-judgment, flag-only.

## Severity taxonomy (verbatim)

**Severity taxonomy (decision 6, verbatim):** `critical` = would change
participant behavior (wrong instruction content, wrong response-scale
semantics, missing condition); `major` = structure diverges, task intact
(randomization flags, block structure); `minor` = metadata (citation,
cosmetic labels); `info` = intentional adaptations.

## Conventions

- Python 3.11+, type hints on everything, docstrings explain *why*.
- `schemas.py` (pydantic v2) is the single source of truth for every run
  artifact; a stage output must pass `python -m coggym_check
  validate-artifact <path>` before that stage counts as done.
- Run metadata lives in `run_meta.json` per run directory.
- coggym.org returns 403 to automated fetchers — don't fetch it.

## venv usage

`source .venv/bin/activate` before running `python -m coggym_check ...` or
`pytest`; dependencies are pinned in `requirements.txt`.
