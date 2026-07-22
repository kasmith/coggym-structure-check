---
name: check-study
description: Validate a CogGym study's structure against its source paper and materials
argument-hint: <StudyName> [--commit <sha>] [--stage <name>] [--force]
---

## Mission

Orchestrate the full structure-check pipeline for one CogGym study: run the
deterministic stages directly, launch the judgment stages as subagents, and
finish with a report + (maybe) a local fix branch. You are the orchestrator —
you never do the paper/materials/comparison/fix judgment work yourself; that
belongs to the named subagents below.

## 0. Parse `$ARGUMENTS`

`$ARGUMENTS` is `<StudyName> [--commit <sha>] [--stage <name>] [--force]`.

- `<StudyName>` (required) — the study folder name under `studies/` in the
  datasets repo, e.g. `Baker2017Rational`.
- `--commit <sha>` (optional) — pin the run to a datasets-repo commit. Every
  CLI call below takes `--commit <sha>` when this is set; the run dir becomes
  `runs/<StudyName>@<shortsha>` instead of `runs/<StudyName>`.
- `--stage <name>` (optional) — one of the registry stage names (`snapshot`,
  `lint`, `paper`, `materials`, `paper_summary`, `materials_summary`,
  `comparison`, `fix`). When given, run **only** that stage (plus the
  render half of `fix`, since `fix` and `render` are handled together — see
  step 3g), regardless of its current status. Note in your final message if
  its declared upstream inputs are themselves missing/stale — the stage will
  still run, but on degraded inputs.
- `--force` (optional) — re-run every stage even if `init-run` reports it
  `done`.

If `<StudyName>` is missing, stop and ask for it — do not guess a study name.

## 1. Initialize the run and read the status table

Run:
```
python -m coggym_check init-run <StudyName> [--commit <sha>]
```
This creates (or reuses) `runs/<StudyName>[@<shortsha>]/` and prints a
`STAGE  STATUS  ARTIFACT` table for every registry stage in pipeline order:
`snapshot, lint, paper, materials, paper_summary, materials_summary,
comparison, fix, render`. Note the printed run-dir path (`$RUN_DIR` below) —
every later command and every subagent prompt uses this exact path. Show the
table to the user before proceeding.

Re-run `init-run` after every stage below to refresh this table — it is the
one source of truth for `done`/`stale`/`missing`/`skipped`/`failed` per
stage, and later stages' "should I run" decisions in step 2 depend on
re-reading it fresh each time (a stage you just made `done` can flip a
downstream stage from `missing` to `stale` because its input hash changed).

## 2. Decide which stages to run

- If `--stage X` was given: run only `X` (and, if `X == "fix"`, also treat
  `render` as covered by the same subagent call — see 3g). Skip this
  decision step entirely otherwise.
- Otherwise: walk the registry in order. For each stage, run it if its
  current status is `missing` or `stale`, or if `--force` was passed (even
  when `done`). Leave a stage alone if it is `done` (and not forced). A
  stage previously `skipped` (per the skip rules in step 3e/3f, recorded via
  `record-stage ... skipped` — see step 3) stays skipped on a plain re-run —
  only `--force` or `--stage` re-attempts it, and the status table genuinely
  shows it as `skipped`, not `missing`. Staleness is real here too: once a
  stage has been recorded (via `record-stage` below, or automatically by the
  deterministic CLI subcommands), a later change to one of its recorded
  upstream inputs flips it to `stale` on the next `init-run`.

## 3. Run each stage

### a. `snapshot` — deterministic, Bash

```
python -m coggym_check snapshot <StudyName> [--commit <sha>]
```
Exit 0: `study_snapshot.json` written — proceed. **Exit 1 is fatal**: the
study doesn't exist in the datasets repo (`StudyNotFoundError`). Stop the
whole orchestration and report this to the user; nothing downstream can run
without a snapshot.

### b. `lint` — deterministic, Bash

```
python -m coggym_check lint <StudyName> [--commit <sha>]
```
Writes `lint.json` and prints a findings table regardless of exit code.
**Exit 1 is not fatal** — it only means at least one `error`-level finding
was produced; those findings are meant to flow into the comparator as
evidence, not to halt the pipeline. Continue either way.

### c. `paper` — deterministic Bash, with an agent fallback

```
python -m coggym_check extract-paper <StudyName> [--commit <sha>]
```
Exit 0 (`status: found_local`): `paper_status.json` written, done — no agent
needed. **Exit 1 (`status: not_found`) is not fatal** — it is exactly the
signal that `paper.pdf` isn't in the dataset and the `paper-finder` agent
must run before the stage can be considered complete. Launch it via the Task
tool:

> Study: `<StudyName>`. Run dir: `$RUN_DIR`. `$RUN_DIR/paper_status.json`
> currently reports `status: not_found` — `extract-paper` found no local
> `paper.pdf`. Find an open-access copy online, get it extracted, and
> overwrite `$RUN_DIR/paper_status.json` with the real outcome
> (`found_online`, `paywalled`, or `not_found`). Finish by running
> `validate-artifact` on it.

After the agent returns, run `python -m coggym_check validate-artifact
$RUN_DIR/paper_status.json` yourself to confirm it's still valid, then
re-read its `status` field (needed for the skip rule in step 4) — the agent
may leave it `paywalled` or `not_found`; both are legitimate terminal
outcomes here, not failures of this stage. Once validated, record it:
```
python -m coggym_check record-stage <StudyName> paper done [--commit <sha>]
```

### d. `materials` — agentic (`materials-scout`)

Launch via Task tool:

> Study: `<StudyName>`. Run dir: `$RUN_DIR`. Discover author-released
> materials for this study (grep `materials/<StudyName>/paper_text/*.txt`
> for OSF/GitHub links, check `$RUN_DIR/study_snapshot.json`'s
> `source_url_csv`, then web-search). Write
> `$RUN_DIR/materials_manifest.json` with every candidate as `pending`, then
> run `python -m coggym_check download <StudyName> [--commit <sha>]`
> yourself to fetch them, and confirm the back-fill. Finish by running
> `validate-artifact` on `$RUN_DIR/materials_manifest.json`.

After it returns: `python -m coggym_check validate-artifact
$RUN_DIR/materials_manifest.json`. Read its `status` field (needed for the
skip rule below). Once validated, record it:
```
python -m coggym_check record-stage <StudyName> materials done [--commit <sha>]
```

### e. `paper_summary` — agentic (`paper-analyst`), skippable

**Skip this stage** if `paper_status.json`'s `status` is `not_found` or
`paywalled` — there is no paper text for `paper-analyst` to read. Leave
`paper_summary.json` unwritten, note the skip for the final summary, and
record it:
```
python -m coggym_check record-stage <StudyName> paper_summary skipped --reason "paper_status is <status>" [--commit <sha>]
```

Otherwise, launch via Task tool:

> Study: `<StudyName>`. Run dir: `$RUN_DIR`. Extract this study's paper
> structure from `materials/<StudyName>/paper_text/page_*.txt` (or
> `materials/<StudyName>/paper.pdf` directly if `$RUN_DIR/paper_status.json`
> says `needs_direct_pdf_read: true`) and map each paper experiment onto the
> CogGym folders in `$RUN_DIR/study_snapshot.json`. Write
> `$RUN_DIR/paper_summary.json`. Finish by running `validate-artifact` on
> it.

After it returns: `python -m coggym_check validate-artifact
$RUN_DIR/paper_summary.json`, then record it:
```
python -m coggym_check record-stage <StudyName> paper_summary done [--commit <sha>]
```

### f. `materials_summary` — agentic (`materials-analyst`), skippable

**Skip this stage** if `materials_manifest.json`'s `status` is
`none_found` — there is nothing downloaded for `materials-analyst` to mine.
Leave `materials_summary.json` unwritten, note the skip for the final
summary, and record it:
```
python -m coggym_check record-stage <StudyName> materials_summary skipped --reason "materials_manifest status is none_found" [--commit <sha>]
```

Otherwise, launch via Task tool:

> Study: `<StudyName>`. Run dir: `$RUN_DIR`. Mine the materials listed in
> `$RUN_DIR/materials_manifest.json` (only `download_status: "ok"` or
> `"skipped_too_large"` sources) for verbatim instruction text,
> randomization logic, response-scale details, and stimulus lists, per
> CogGym experiment folder in `$RUN_DIR/study_snapshot.json`. Write
> `$RUN_DIR/materials_summary.json`. Finish by running `validate-artifact`
> on it.

After it returns: `python -m coggym_check validate-artifact
$RUN_DIR/materials_summary.json`, then record it:
```
python -m coggym_check record-stage <StudyName> materials_summary done [--commit <sha>]
```

### g. `comparison` — agentic (`structure-comparator`), always runs

Never skip this stage, even if both `paper_summary` and `materials_summary`
were skipped above — a comparison with no paper/materials coverage is still
meaningful (it reports on `study_snapshot.json` + `lint.json` alone, and its
`coverage.paper`/`coverage.materials` fields communicate the degradation
honestly). Launch via Task tool:

> Study: `<StudyName>`. Run dir: `$RUN_DIR`. Compare
> `$RUN_DIR/study_snapshot.json`, `$RUN_DIR/lint.json`,
> `$RUN_DIR/paper_summary.json` (if present), and
> `$RUN_DIR/materials_summary.json` (if present) field-by-field, applying the
> fix-conservatism and severity-taxonomy policies in `constraints.md`
> exactly as written. Write `$RUN_DIR/comparison.json`. Finish by running
> `validate-artifact` on it.

After it returns: `python -m coggym_check validate-artifact
$RUN_DIR/comparison.json`. Read `clear_cut_count` /
`needs_human_judgment_count` off it — you need these for the final message.
Then record it:
```
python -m coggym_check record-stage <StudyName> comparison done [--commit <sha>]
```

### h. `fix` + `render` — one agentic step (`fix-drafter`)

`fix-drafter` runs `apply-fixes` and `render` itself, so treat the registry's
`fix` and `render` stages as a single unit here. Launch via Task tool:

> Study: `<StudyName>`. Run dir: `$RUN_DIR`. Filter
> `$RUN_DIR/comparison.json`'s discrepancies to those with `clear_cut: true`
> and `recommendation: "fix"`. Write `$RUN_DIR/fix_plan.json`, then run
> `python -m coggym_check apply-fixes <StudyName> [--commit <sha>]` and
> `python -m coggym_check render <StudyName> [--commit <sha>]` yourself.
> Report back the branch name (or that none was created), the number of
> fixes applied, and the number left flagged for human judgment. Finish by
> running `validate-artifact` on `$RUN_DIR/fix_plan.json`.

After it returns, verify **both** artifacts it is responsible for:
`python -m coggym_check validate-artifact $RUN_DIR/fix_plan.json`, and
confirm `$RUN_DIR/report.md` exists (`report.md` has no pydantic schema —
its presence alone is the check). Also confirm `$RUN_DIR/pr.md` exists.
Once both are confirmed, record both stages:
```
python -m coggym_check record-stage <StudyName> fix done [--commit <sha>]
python -m coggym_check record-stage <StudyName> render done [--commit <sha>]
```

If `apply-fixes` refused because there is no lint baseline (`lint.json`
missing) — this should never happen since stage `b` always runs lint first
— tell the subagent (or run yourself) `python -m coggym_check lint
<StudyName> [--commit <sha>]` and have it retry. If `apply-fixes` aborted
because a fix introduced a lint regression, that is a genuine failure to
surface to the user, not something to silently retry with a different fix
set.

## 4. Re-check status after each stage

After every stage above, re-run `python -m coggym_check init-run <StudyName>
[--commit <sha>]` and glance at the refreshed table before deciding what's
next. Abort the whole run only on the snapshot's hard failure (step 3a);
every other soft failure (lint errors, paper not_found, materials
none_found, a dropped fix) means "continue, degraded" — record it and keep
going, never silently upgrade a degraded outcome to look clean in the final
message.

## 5. Final message

Always end with:
1. The report path: `$RUN_DIR/report.md`.
2. The branch name if `fix-drafter` created one (`fix/<StudyName>-structure`),
   or a note that no branch was created (zero clear-cut fixes).
3. Auto-fix vs needs-human-judgment counts — pull `clear_cut_count` /
   `needs_human_judgment_count` from `comparison.json` and the applied/dropped
   counts fix-drafter reported.
4. Which stages were skipped and why (`paper_summary`/`materials_summary`,
   per step 3e/3f) if any were.
5. The verbatim closing line, exactly:

   The branch is local only — review report.md before pushing anything.
