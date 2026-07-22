"""Render `comparison.json` (+ optional `fix_plan.json`/`paper_status.json`/
`materials_manifest.json`) into a human-facing `report.md` and a
PR-description `pr.md`.

Why `render()` is a pure function of already-loaded pydantic models plus an
explicit `generated_at` string (rather than reading files and calling
`datetime.now()` itself): the CLI's `render` subcommand supplies the current
UTC time and reads whichever optional artifacts exist in the run dir, but
golden tests need byte-exact, reproducible output — passing `generated_at`
in and taking already-parsed models out of the equation keeps this module
untestable-by-nondeterminism-only, not untestable at all.

Degradation policy: `fix_plan`/`paper_status`/`materials_manifest` are each
`None`-able. Missing ones are never treated as errors — the report says so
explicitly ("not available for this run") wherever that artifact's section
would otherwise go, since a `render` may run before those upstream stages
did (or after they were explicitly skipped, e.g. no paper found at all).

Two conventions this module introduces (not specified by an upstream
schema, so pinned here and documented rather than left ambiguous):

- **Structure summary** (report.md, per experiment): populated from
  `Confirmation` entries whose `field` is one of `_STRUCTURE_SUMMARY_FIELDS`
  below. A comparator is expected to always emit a confirmation for each of
  these when it isn't itself under dispute; a metric missing from
  confirmations (typically because it *is* a discrepancy instead) renders
  as "see discrepancies" rather than blank.
- **Auto-fixed vs. needs-human-judgment split**: purely `Discrepancy.
  recommendation` ("fix" -> auto-fixed table, "flag" -> needs-human-judgment
  table), independent of whether `fix_plan` later dropped a "fix"
  recommendation — a dropped fix is still surfaced, in the "Fix plan"
  section's "Declined fixes" list, so nothing silently disappears.
"""

from __future__ import annotations

from coggym_check import __version__, schemas

#: Confirmation.field names the "Structure summary" table looks for, in
#: display order. See module docstring's "Structure summary" convention.
_STRUCTURE_SUMMARY_FIELDS = [
    ("n_conditions", "Conditions"),
    ("n_trials", "Trials"),
    ("n_instructions", "Instructions"),
    ("response_types", "Response type(s)"),
]

#: FixOp.op -> the one constraints.md fix-conservatism category it always
#: belongs to (the four auto-fixable categories map 1:1 onto the four FixOp
#: types), used both here (grouping the PR body's "Auto-fixed" summary) and
#: in fixer.py (grouping commits) -- kept as a private duplicate rather than
#: a shared import so report.py has zero dependency on fixer.py.
_OP_CATEGORY = {
    "set_instruction_text": "instruction_text",
    "set_block_randomization": "randomization",
    "set_slider_labels": "response_scale",
    "set_config_field": "citation",
}

_TAGLINE = "🤖 Generated with coggym-structure-check (review before submitting)"

#: Category -> short human phrase, used in the PR body's "Auto-fixed" bullets.
_CATEGORY_PHRASE = {
    "instruction_text": "instruction text corrected to match verbatim materials",
    "randomization": "block randomization flags corrected",
    "response_scale": "slider labels/range corrected",
    "citation": "citation metadata corrected",
}


def _fmt_value(value: object) -> str:
    """Render an arbitrary JSON-ish value as one inline Markdown-table cell.

    `None` -> the em dash (distinguishing "confirmed absent" from "no
    evidence available" would need more context than a table cell can
    carry); everything else -> `str()`, with literal `|`/newline escaped so
    a multi-line quote or a value containing a pipe can never break a
    Markdown table row.
    """
    if value is None:
        return "—"
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """Render a GitHub-flavored Markdown table as a list of lines."""
    lines = [f"| {' | '.join(header)} |", f"| {' | '.join(['---'] * len(header))} |"]
    for row in rows:
        lines.append(f"| {' | '.join(row)} |")
    return lines


def _coverage_word(level: str) -> str:
    return {"full": "full", "partial": "partial", "none": "none"}.get(level, level)


def _render_coverage_section(coverage: schemas.Coverage) -> list[str]:
    lines = [
        "## Coverage",
        "",
        f"- **Paper evidence:** {_coverage_word(coverage.paper)}",
        f"- **Materials evidence:** {_coverage_word(coverage.materials)}",
        "",
    ]
    degraded = [
        source
        for source, level in (("paper", coverage.paper), ("materials", coverage.materials))
        if level != "full"
    ]
    if degraded:
        lines.append(
            "> **Coverage caveat:** "
            + " and ".join(degraded)
            + " evidence is incomplete for this study — treat every discrepancy"
            " below (and every confirmation) as correspondingly less certain."
        )
        lines.append("")
    return lines


def _render_paper_status_section(paper_status: schemas.PaperStatus | None) -> list[str]:
    lines = ["### Paper status", ""]
    if paper_status is None:
        lines.append("_paper_status.json not available for this run._")
        lines.append("")
        return lines
    lines.append(f"- **Status:** {paper_status.status}")
    lines.append(f"- **PDF path:** {_fmt_value(paper_status.pdf_path)}")
    lines.append(f"- **Pages:** {_fmt_value(paper_status.n_pages)}")
    lines.append(f"- **Text quality:** {_fmt_value(paper_status.text_quality)}")
    lines.append(f"- **Needs direct PDF read:** {paper_status.needs_direct_pdf_read}")
    if paper_status.notes:
        lines.append(f"- **Notes:** {'; '.join(paper_status.notes)}")
    lines.append("")
    return lines


def _render_materials_manifest_section(
    materials_manifest: schemas.MaterialsManifest | None,
) -> list[str]:
    lines = ["### Materials manifest", ""]
    if materials_manifest is None:
        lines.append("_materials_manifest.json not available for this run._")
        lines.append("")
        return lines
    lines.append(f"- **Status:** {materials_manifest.status}")
    lines.append(f"- **Sources found:** {len(materials_manifest.sources)}")
    ok = sum(1 for s in materials_manifest.sources if s.download_status == "ok")
    lines.append(f"- **Sources downloaded ok:** {ok}/{len(materials_manifest.sources)}")
    if materials_manifest.notes:
        lines.append(f"- **Notes:** {'; '.join(materials_manifest.notes)}")
    lines.append("")
    return lines


def _render_fix_plan_section(fix_plan: schemas.FixPlan | None) -> list[str]:
    lines = ["## Fix plan", ""]
    if fix_plan is None:
        lines.append("_fix_plan.json not available for this run — no fixes were drafted._")
        lines.append("")
        return lines
    lines.append(f"- **Fixes applied:** {len(fix_plan.fixes)}")
    lines.append(f"- **Fixes declined:** {len(fix_plan.dropped)}")
    lines.append("")
    if fix_plan.dropped:
        lines.append("### Declined fixes")
        lines.append("")
        for dropped in sorted(fix_plan.dropped, key=lambda d: d.discrepancy_id):
            lines.append(f"- **{dropped.discrepancy_id}**: {dropped.reason}")
        lines.append("")
    return lines


def _structure_summary_rows(confirmations: list[schemas.Confirmation]) -> list[list[str]]:
    by_field = {c.field: c for c in confirmations}
    rows = []
    for field, label in _STRUCTURE_SUMMARY_FIELDS:
        confirmation = by_field.get(field)
        if confirmation is None:
            rows.append([label, "see discrepancies", "—"])
        else:
            rows.append(
                [label, _fmt_value(confirmation.value), _fmt_value(confirmation.evidence_ref)]
            )
    return rows


def _discrepancy_row(d: schemas.Discrepancy) -> list[str]:
    return [
        d.id,
        d.category,
        d.severity,
        _fmt_value(d.field),
        _fmt_value(d.paper_value),
        _fmt_value(d.coggym_value),
        _fmt_value(d.rationale),
    ]


_DISCREPANCY_HEADER = [
    "ID",
    "Category",
    "Severity",
    "Field",
    "Paper value",
    "CogGym value",
    "Rationale",
]


def _render_experiment_section(exp: schemas.ComparisonExperiment) -> list[str]:
    lines = [f"### {exp.folder}", "", "#### Structure summary", ""]
    lines.extend(_md_table(["Metric", "Value", "Evidence"], _structure_summary_rows(exp.confirmations)))
    lines.append("")

    lines.append("#### Confirmations")
    lines.append("")
    if exp.confirmations:
        rows = [
            [c.field, _fmt_value(c.value), _fmt_value(c.evidence_ref)]
            for c in exp.confirmations
        ]
        lines.extend(_md_table(["Field", "Value", "Evidence ref"], rows))
    else:
        lines.append("_no confirmed fields recorded._")
    lines.append("")

    fixed = [d for d in exp.discrepancies if d.recommendation == "fix"]
    flagged = [d for d in exp.discrepancies if d.recommendation == "flag"]

    lines.append("#### Discrepancies — Auto-fixed")
    lines.append("")
    if fixed:
        lines.extend(_md_table(_DISCREPANCY_HEADER, [_discrepancy_row(d) for d in fixed]))
    else:
        lines.append("_none._")
    lines.append("")

    lines.append("#### Discrepancies — Needs human judgment")
    lines.append("")
    if flagged:
        lines.extend(_md_table(_DISCREPANCY_HEADER, [_discrepancy_row(d) for d in flagged]))
    else:
        lines.append("_none._")
    lines.append("")

    return lines


def _render_evidence_appendix(experiments: list[schemas.ComparisonExperiment]) -> list[str]:
    lines = ["## Evidence appendix", ""]
    for exp in experiments:
        if not exp.discrepancies:
            continue
        lines.append(f"### {exp.folder}")
        lines.append("")
        for d in exp.discrepancies:
            lines.append(f"**{d.id}** ({d.category}, {d.severity})")
            lines.append("")
            if d.evidence:
                for ev in d.evidence:
                    lines.append(f"- [{ev.source}] \"{ev.quote}\" ({ev.ref})")
            else:
                lines.append("- _no evidence recorded._")
            lines.append("")
    return lines


def render(
    comparison: schemas.Comparison,
    fix_plan: schemas.FixPlan | None,
    paper_status: schemas.PaperStatus | None,
    materials_manifest: schemas.MaterialsManifest | None,
    generated_at: str,
) -> tuple[str, str]:
    """Render `(report.md, pr.md)` for one study's comparison run.

    Pure function of already-validated artifacts + an explicit timestamp —
    see module docstring for why (golden-test reproducibility).
    """
    report_lines: list[str] = [
        f"# Structure Check Report: {comparison.study}",
        "",
        f"- **Study:** {comparison.study}",
        f"- **Commit:** {_fmt_value(comparison.commit)}",
        f"- **Generated:** {generated_at}",
        f"- **Tool version:** {__version__}",
        "",
    ]
    report_lines.extend(_render_coverage_section(comparison.coverage))
    report_lines.extend(_render_paper_status_section(paper_status))
    report_lines.extend(_render_materials_manifest_section(materials_manifest))
    report_lines.extend(_render_fix_plan_section(fix_plan))

    report_lines.append("## Experiments")
    report_lines.append("")
    for exp in comparison.experiments:
        report_lines.extend(_render_experiment_section(exp))

    report_lines.extend(_render_evidence_appendix(comparison.experiments))

    report_md = "\n".join(report_lines).rstrip("\n") + "\n"

    pr_md = _render_pr_md(comparison, fix_plan)

    return report_md, pr_md


def _render_pr_md(
    comparison: schemas.Comparison, fix_plan: schemas.FixPlan | None
) -> str:
    title = fix_plan.pr_title if fix_plan is not None else f"Structure check: {comparison.study}"
    lines = [f"# {title}", ""]

    all_flagged = sorted(
        (
            (exp.folder, d)
            for exp in comparison.experiments
            for d in exp.discrepancies
            if d.recommendation == "flag"
        ),
        key=lambda pair: (pair[0], pair[1].id),
    )

    if fix_plan is None:
        lines.append(
            "_fix_plan.json was not available for this run; no automatic fixes were applied._"
        )
        lines.append("")
    elif not fix_plan.fixes:
        lines.append("No auto-fixable discrepancies were found.")
        lines.append("")
    else:
        n_fixes = len(fix_plan.fixes)
        lines.append(
            f"This PR applies {n_fixes} automatic fix"
            f"{'es' if n_fixes != 1 else ''} to `{comparison.study}` "
            f"and flags {len(all_flagged)} discrepanc"
            f"{'ies' if len(all_flagged) != 1 else 'y'} for human review."
        )
        lines.append("")
        lines.append("## Auto-fixed")
        lines.append("")

        by_category: dict[str, list[schemas.FixEntry]] = {}
        for entry in fix_plan.fixes:
            category = _OP_CATEGORY[entry.fix.op]
            by_category.setdefault(category, []).append(entry)
        for category in sorted(by_category):
            entries = sorted(by_category[category], key=lambda e: e.discrepancy_id)
            ids = ", ".join(e.discrepancy_id for e in entries)
            phrase = _CATEGORY_PHRASE.get(category, category)
            lines.append(f"- **{category}** ({phrase}): {ids}")
        lines.append("")

        if fix_plan.dropped:
            lines.append("## Declined fixes")
            lines.append("")
            for dropped in sorted(fix_plan.dropped, key=lambda d: d.discrepancy_id):
                lines.append(f"- **{dropped.discrepancy_id}**: {dropped.reason}")
            lines.append("")

    lines.append("## Needs human judgment")
    lines.append("")
    if all_flagged:
        for folder, d in all_flagged:
            rationale = d.rationale.replace("\n", " ")
            lines.append(f"- [ ] **{d.id}** ({d.severity}, {d.category}): {rationale}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append(_TAGLINE)

    return "\n".join(lines).rstrip("\n") + "\n"
