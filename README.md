# coggym-structure-check

An agentic system that validates CogGym experiment implementations (in the
`cog-gym-datasets` repo) against the structure described in their source
papers and author-released OSF/GitHub materials. The pipeline is staged:
each stage writes one validated JSON/Markdown artifact to
`runs/<study>[@<shortsha>]/`; deterministic work (parsing, linting, PDF
extraction, downloading, applying fixes, rendering) is plain Python in
`src/coggym_check/`, and judgment work (reading a paper, mining materials,
comparing structure, drafting fixes) is done by Claude subagents in
`.claude/agents/*.md`, orchestrated either interactively by the
`/check-study` skill or headlessly by `python -m coggym_check run`.

## Quickstart

```bash
source .venv/bin/activate
pip install -e .
```

Then check a study either interactively, inside a Claude Code session in
this repo:

```
/check-study Gerstenberg2015How
```

or headlessly, from a plain shell (shells out to the `claude` CLI itself for
each agentic stage):

```bash
python -m coggym_check run Gerstenberg2015How
```

Both paths run the same nine-stage pipeline (see [Pipeline](#pipeline)
below) and produce the same run-dir artifacts.

## CLI subcommands

Every subcommand takes a `--commit <sha>` option (except `validate-artifact`
and `export-schemas`) to pin the run dir to a datasets-repo commit
(`runs/<study>@<shortsha>`); omit it to use the working tree
(`runs/<study>`).

| Subcommand | What it does | Notable flags |
|---|---|---|
| `snapshot <study>` | Parse a study into a validated `study_snapshot.json`. | `--commit` |
| `lint <study>` | Run structural lint over a study and write a validated `lint.json`; prints a findings table. | `--commit` |
| `extract-paper <study>` | Locate + extract a study's `paper.pdf` (local only) and write `paper_status.json`. | `--commit` |
| `download <study>` | Download every `pending` source in a study's `materials_manifest.json`, back-filling it. | `--commit` |
| `render <study>` | Render `comparison.json` (+ optional `fix_plan`/`paper_status`/`materials_manifest`) into `report.md` + `pr.md`. | `--commit` |
| `apply-fixes <study>` | Apply `fix_plan.json`'s fixes to the datasets repo via a `git worktree` + local branch (never the main working tree, never pushed). | `--commit`, `--dry-run`, `--force-branch` |
| `run [study]` | Headless: run every pending stage of the full pipeline for one study, or every study named in a file. | `--commit`, `--force`, `--studies-from FILE`, `--max-turns N` |
| `init-run <study>` | Create (or reuse) a run dir + `run_meta.json` and print its stage status table (`done`/`stale`/`missing`/`skipped`/`failed`). | `--commit` |
| `validate-artifact <path>` | Validate one artifact JSON file against the pydantic schema its filename implies. | — |
| `export-schemas` | Render every artifact model's JSON Schema + purpose into `docs/artifacts.md`. | `--out PATH` |

Run `python -m coggym_check <subcommand> --help` for the exact usage of any
of these.

## Pipeline

See [`docs/pipeline.md`](docs/pipeline.md) for the full stage diagram, the
stage-by-stage producer/input/artifact/skip-condition table, the
failure-mode matrix (paper not found, paywalled, no materials, lint
regression, existing branch, etc.), resumability semantics, and agent model
assignments. In brief, the nine registry stages
(`src/coggym_check/rundir.py`'s `STAGE_REGISTRY`) run in this order:
`snapshot`, `lint`, `paper`, `materials`, `paper_summary`,
`materials_summary`, `comparison`, `fix`, `render`.

## Outputs

A completed run of `runs/<study>[@<shortsha>]/` leaves:

- **`report.md`** — the human-readable structure-check report: confirmations,
  discrepancies (with severity), auto-fixed vs. needs-human-judgment counts,
  and coverage notes for any skipped stages.
- **`pr.md`** — a draft PR title + body summarizing the fixes applied, ready
  to hand to `gh pr create` or paste into GitHub's PR form.
- **A local fix branch**, `fix/<study>-structure`, in the `cog-gym-datasets`
  clone (default `../cog-gym-datasets`, overridable via
  `COGGYM_DATASETS_REPO`) — created only if at least one clear-cut,
  fix-recommended discrepancy was found. **This branch is local only.**
  `apply-fixes` never runs `git push`; getting it onto GitHub is always a
  manual step (below).

## Submitting the PR yourself

The tool never pushes anything. Once you've reviewed `report.md` and are
happy with the fix branch:

```bash
cd ../cog-gym-datasets   # or wherever COGGYM_DATASETS_REPO points
git push -u origin fix/<study>-structure
PR_MD=../coggym-structure-check/runs/<study>/pr.md
TITLE=$(sed -n '1s/^# //p' "$PR_MD")   # pr.md's first line is "# <title>"
gh pr create --title "$TITLE" --body-file "$PR_MD"
```

(Or just open `pr.md` and copy/paste its title/body into GitHub's PR form
by hand. `pr.md` and `report.md` live in this repo's `runs/<study>/`, not in
the datasets repo.)

## Path contract

- Datasets repo lives at `../cog-gym-datasets` (sibling of this repo),
  overridable via env `COGGYM_DATASETS_REPO`.
- The clone is on a **detached HEAD** with untracked files. **NEVER** modify
  its working tree, index, or HEAD directly — no edits, no `git add`/
  `commit`/`checkout`/`reset` there.
- All writes to that repo happen via `python -m coggym_check apply-fixes`,
  which uses a `git worktree` internally. **Never `git push`** from that repo
  — see "Submitting the PR yourself" above.

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
