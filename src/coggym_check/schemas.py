"""Pydantic v2 models for every run artifact — the single source of truth.

Why this module exists (constraints.md hard rule #2): every pipeline stage
(deterministic Python or Claude subagent) writes exactly one JSON artifact,
and every later stage reads it back. If field names/types drifted between
producer and consumer there would be no single place to catch it. Instead,
every artifact is validated through one of the models below via
``python -m coggym_check validate-artifact <path>`` before a stage counts as
done, and ``export-schemas`` renders these same models into
``docs/artifacts.md`` so humans and subagents share one description of the
contract.

Design notes:
- All models forbid extra fields (``extra="forbid"``) so a producer's typo
  (wrong key name) surfaces immediately as a validation error rather than
  being silently dropped.
- ``Sourced`` is a plain (non-generic) model with ``value: Any | None``
  rather than a pydantic ``Generic[T]``, per the task's explicit guidance:
  a generic would need a type parameter baked into the JSON Schema export,
  which complicates docs generation for no benefit here (every consumer
  already knows what type it expects from context).
- ``FixOp`` is a discriminated union (on the ``op`` literal) of four
  operation types. These four are exhaustive by design (task-2 brief): no
  fifth op should ever be added without a schema change.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArtifactModel(BaseModel):
    """Common base for every artifact (and nested) model.

    ``extra="forbid"`` is the one behavior every model in this file shares:
    an unexpected key almost always means a producer wrote the wrong field
    name, and silently ignoring it would let bad data flow downstream
    undetected.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Sourced wrapper (constraints.md's `Sourced` spec, used across PaperSummary
# and MaterialsSummary to tie every claim back to evidence).
# ---------------------------------------------------------------------------


class Sourced(ArtifactModel):
    """A single claim tied to its supporting evidence (or lack thereof).

    ``confidence``/``source`` are required (no default): every claim must be
    explicitly tagged, even when there is no quote to point to (in which
    case the producing agent is expected to use
    ``source="inferred", confidence="low"`` rather than omitting the tag).
    """

    value: Any | None
    quote: str | None
    ref: str | None
    confidence: Literal["high", "medium", "low"]
    source: Literal["paper", "materials", "inferred"]


# ---------------------------------------------------------------------------
# StudySnapshot (study_snapshot.json)
# ---------------------------------------------------------------------------


class Citation(ArtifactModel):
    """Study-level citation metadata. All fields optional: 6/265 configs in
    the dataset lack citation info entirely, and this model must still
    validate for those studies (as an all-None object).
    """

    paper_title: str | None = None
    citation: str | None = None
    paperDOI: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None


class ConditionSnapshot(ArtifactModel):
    """One experimental condition's block structure."""

    name: str
    n_blocks: int
    block_sizes: list[int]
    block_randomization: list[bool] | None
    block_kinds: list[Literal["instruction", "trial", "mixed"]]


class SliderLabel(ArtifactModel):
    """One labeled point on a slider's range."""

    value: Any
    label: str


class SliderSummary(ArtifactModel):
    """A slider query's numeric range and labeled anchors."""

    min: float
    max: float
    labels: list[SliderLabel]


class QuerySummary(ArtifactModel):
    """Aggregated shape of every query sharing one tag across an experiment."""

    type: str
    options: list[Any] | None
    slider: SliderSummary | None
    n_trials_using: int


class HumanDataSummary(ArtifactModel):
    """Header counts from human_data_mean.json / human_data_ind.json.

    Both fields are optional: malformed or missing header values must not
    crash the snapshot (they are tolerated here and surfaced by lint
    instead).
    """

    participants_count: int | None
    judgment_count: int | None


class ExperimentSnapshot(ArtifactModel):
    """Deterministic parse of one experiment folder's config/trial/instruction files."""

    experiment_name: str
    description: str
    task_type: list[str]
    response_type: list[str]
    stimuli_count: int | None
    n_trials_in_jsonl: int
    n_instructions: int
    conditions: list[ConditionSnapshot]
    queries_by_tag: dict[str, QuerySummary]
    instruction_texts: dict[str, str]
    stimuli_modalities: list[str]
    assets_present: bool
    extra_files: list[str]
    human_data: HumanDataSummary


class StudySnapshot(ArtifactModel):
    """Stage 0 output: a full deterministic parse of one study."""

    study: str
    commit: str | None
    generated_at: str
    tool_version: str
    paper_pdf_present: bool
    source_url_csv: str | None
    citation: Citation
    experiments: dict[str, ExperimentSnapshot]


# ---------------------------------------------------------------------------
# LintReport (lint.json)
# ---------------------------------------------------------------------------


class LintFinding(ArtifactModel):
    """One structural lint finding."""

    check_id: str
    level: Literal["error", "warning", "info"]
    experiment: str | None
    message: str
    location: str | None


class LintReport(ArtifactModel):
    """Stage 0 output: deterministic structural lint findings for a study."""

    study: str
    checks_run: list[str]
    findings: list[LintFinding]


# ---------------------------------------------------------------------------
# PaperStatus (paper_status.json)
# ---------------------------------------------------------------------------


class PaperCandidate(ArtifactModel):
    """One URL tried while looking for an open-access copy of the paper."""

    url: str
    outcome: str


class PaperStatus(ArtifactModel):
    """Stage 1 output: where the paper PDF came from and how well it extracts."""

    study: str
    status: Literal["found_local", "found_online", "paywalled", "not_found"]
    pdf_path: str | None
    sha256: str | None
    retrieved_from: str | None
    candidates_tried: list[PaperCandidate]
    n_pages: int | None
    text_quality: float | None
    needs_direct_pdf_read: bool
    notes: list[str]


# ---------------------------------------------------------------------------
# MaterialsManifest (materials_manifest.json)
# ---------------------------------------------------------------------------


class MaterialsSearch(ArtifactModel):
    """One search query the materials-scout agent tried."""

    engine: str
    query: str
    useful: bool


class MaterialsSource(ArtifactModel):
    """One candidate materials source (OSF node, GitHub repo, or bare URL)."""

    kind: Literal["osf", "github", "other_url"]
    url: str
    relation: Literal["official_materials", "official_code", "replication", "uncertain"]
    evidence: str
    local_path: str | None
    download_status: Literal["pending", "ok", "failed", "skipped_too_large"]
    sha256_or_commit: str | None


class MaterialsManifest(ArtifactModel):
    """Stage 2 output: discovered (and, once downloaded, back-filled) materials sources."""

    study: str
    status: Literal["found", "none_found", "partial"]
    searches: list[MaterialsSearch]
    sources: list[MaterialsSource]
    notes: list[str]


# ---------------------------------------------------------------------------
# PaperSummary (paper_summary.json)
# ---------------------------------------------------------------------------


class PaperExperiment(ArtifactModel):
    """One paper-described experiment, mapped (if possible) to a CogGym folder.

    Every substantive field is a ``Sourced`` wrapper so paper-analyst's claim
    always carries its verbatim quote + page ref (or an explicit
    ``source="inferred", confidence="low"`` when there is none).
    """

    paper_label: str
    coggym_folder: str | None
    mapping_confidence: Literal["high", "medium", "low"]
    mapping_rationale: str
    n_participants: Sourced
    n_conditions: Sourced
    condition_names: Sourced
    design: Sourced
    n_trials_total: Sourced
    n_trials_per_participant: Sourced
    randomization: Sourced
    counterbalancing: Sourced
    instructions_expectations: Sourced
    response_scale: Sourced
    stimuli_modality: Sourced
    stimulus_description: Sourced
    exclusions: Sourced
    notes: list[str]


class PaperSummary(ArtifactModel):
    """Stage 3 output: paper-analyst's structured extraction + experiment mapping."""

    study: str
    paper_sha256: str | None
    experiments: list[PaperExperiment]
    unmapped_paper_experiments: list[str]
    unmapped_coggym_experiments: list[str]
    extraction_issues: list[str]


# ---------------------------------------------------------------------------
# MaterialsSummary (materials_summary.json)
# ---------------------------------------------------------------------------


class VerbatimInstruction(ArtifactModel):
    """One verbatim instruction/consent passage mined from materials.

    Never a paraphrase: ``source_file`` + ``selector_or_lines`` must let a
    human re-locate the exact text this was copied from.
    """

    order_hint: int | None
    text: str
    source_file: str
    selector_or_lines: str | None


class RandomizationEvidence(ArtifactModel):
    """One piece of code/text evidence about randomization logic in materials."""

    claim: str
    source_file: str
    lines: str | None
    quote: str


class StimulusList(ArtifactModel):
    """Where materials' own stimulus list lives, and how many entries it has."""

    n: int | None
    source_file: str | None


class MaterialsExperiment(ArtifactModel):
    """One CogGym experiment's worth of evidence mined from downloaded materials."""

    coggym_folder: str
    matched: bool
    matched_material_paths: list[str]
    verbatim_instructions: list[VerbatimInstruction]
    randomization_evidence: list[RandomizationEvidence]
    response_scale_evidence: list[Sourced]
    stimulus_list: StimulusList | None
    condition_assignment: Sourced | None
    notes: list[str]


class MaterialsSummary(ArtifactModel):
    """Stage 4 output: materials-analyst's verbatim evidence per experiment."""

    study: str
    status: Literal["complete", "partial", "skipped"]
    experiments: list[MaterialsExperiment]
    unmatched_materials: list[str]


# ---------------------------------------------------------------------------
# FixOp (used inside Comparison.Discrepancy.proposed_fix and FixPlan.fixes)
# ---------------------------------------------------------------------------


class SetConfigField(ArtifactModel):
    """Overwrite one dotted key path inside an experiment's config.json."""

    op: Literal["set_config_field"]
    experiment: str
    key_path: str
    new_value: Any


class SetInstructionText(ArtifactModel):
    """Overwrite one instruction.jsonl record's text field."""

    op: Literal["set_instruction_text"]
    experiment: str
    record_id: str
    new_value: str


class SetBlockRandomization(ArtifactModel):
    """Overwrite one experimentFlow condition's block_randomization entry."""

    op: Literal["set_block_randomization"]
    experiment: str
    condition_index: int
    block_index: int
    new_value: bool


class SetSliderLabels(ArtifactModel):
    """Overwrite one trial query's slider labels and/or min/max range."""

    op: Literal["set_slider_labels"]
    experiment: str
    trial_id: str
    query_tag: str
    new_labels: list[Any] | None
    new_min: float | None
    new_max: float | None


#: The four exhaustive fix operations, discriminated on ``op``. Deliberately
#: not itself a BaseModel: it is used as a field annotation (e.g.
#: ``proposed_fix: FixOp | None``), and pydantic's discriminated-union
#: support renders this as a clean `oneOf` + discriminator in JSON Schema
#: without any generic machinery.
FixOp = Annotated[
    Union[SetConfigField, SetInstructionText, SetBlockRandomization, SetSliderLabels],
    Field(discriminator="op"),
]


# ---------------------------------------------------------------------------
# Comparison (comparison.json)
# ---------------------------------------------------------------------------


class Coverage(ArtifactModel):
    """How much of the paper/materials evidence base was actually available."""

    paper: Literal["full", "partial", "none"]
    materials: Literal["full", "partial", "none"]


class DiscrepancyLocation(ArtifactModel):
    """Where in the CogGym implementation a discrepancy was found."""

    file: str
    record_id: str | None
    json_path: str | None


class DiscrepancyEvidence(ArtifactModel):
    """One quote+ref backing a discrepancy, tagged by which artifact it came from."""

    source: Literal["paper", "materials", "lint"]
    quote: str
    ref: str


class Discrepancy(ArtifactModel):
    """One field-level mismatch between paper/materials evidence and the
    CogGym implementation.

    The validator below enforces constraints.md's fix-conservatism policy at
    the schema level: a comparator can only recommend "fix" when it is both
    clear-cut AND has committed to a concrete, expressible fix operation.
    Anything else must be "flag" (needs-human-judgment).
    """

    id: str
    category: Literal[
        "instruction_text",
        "randomization",
        "trial_count",
        "condition_structure",
        "response_scale",
        "citation",
        "stimuli",
        "lint",
        "other",
    ]
    field: str
    location: DiscrepancyLocation
    paper_value: Any
    materials_value: Any
    coggym_value: Any
    severity: Literal["critical", "major", "minor", "info"]
    clear_cut: bool
    evidence: list[DiscrepancyEvidence]
    recommendation: Literal["fix", "flag"]
    rationale: str
    proposed_fix: FixOp | None

    @model_validator(mode="after")
    def _fix_requires_clear_cut_and_proposed_fix(self) -> "Discrepancy":
        """``recommendation == "fix"`` requires ``clear_cut`` and a fix.

        Why a schema-level validator rather than leaving this to the
        comparator's prompt: this is a hard safety rule (constraints.md's
        fix-conservatism policy), and violations must be impossible to write
        to disk, not just discouraged in a system prompt.
        """
        if self.recommendation == "fix":
            if not self.clear_cut:
                raise ValueError(
                    "Discrepancy.recommendation == 'fix' requires clear_cut == True"
                )
            if self.proposed_fix is None:
                raise ValueError(
                    "Discrepancy.recommendation == 'fix' requires proposed_fix to be set"
                )
        return self


class Confirmation(ArtifactModel):
    """One field the comparator confirmed matches across sources (no discrepancy)."""

    field: str
    value: Any
    evidence_ref: str


class ComparisonExperiment(ArtifactModel):
    """One experiment's discrepancies and confirmations."""

    folder: str
    discrepancies: list[Discrepancy]
    confirmations: list[Confirmation]


class Comparison(ArtifactModel):
    """Stage 5 output: field-by-field comparison across paper/materials/CogGym."""

    study: str
    commit: str | None
    inputs: dict[str, str | None]
    coverage: Coverage
    experiments: list[ComparisonExperiment]
    needs_human_judgment_count: int
    clear_cut_count: int


# ---------------------------------------------------------------------------
# FixPlan (fix_plan.json)
# ---------------------------------------------------------------------------


class FixEntry(ArtifactModel):
    """One fix operation carried over from a clear-cut discrepancy."""

    fix: FixOp
    discrepancy_id: str
    commit_message: str


class DroppedFix(ArtifactModel):
    """One proposed fix the fix-drafter declined to apply, and why."""

    discrepancy_id: str
    reason: str


class FixPlan(ArtifactModel):
    """Stage 6 output: the filtered, safe-to-apply subset of discrepancies."""

    study: str
    fixes: list[FixEntry]
    dropped: list[DroppedFix]
    pr_title: str
    pr_body: str


# ---------------------------------------------------------------------------
# RunMeta (run_meta.json)
# ---------------------------------------------------------------------------


class ClaudeInvocationMeta(ArtifactModel):
    """Cost/duration/session bookkeeping for one agentic stage's Claude call."""

    cost_usd: float
    duration_s: float
    session_id: str


class StageMeta(ArtifactModel):
    """One pipeline stage's recorded status for resumability (rundir.py)."""

    status: Literal["done", "skipped", "failed"]
    artifact: str | None
    input_hashes: dict[str, str]
    skipped_reason: str | None
    claude: ClaudeInvocationMeta | None


class RunMeta(ArtifactModel):
    """Written/updated by every stage: per-stage status for one run directory."""

    study: str
    commit: str | None
    created_at: str
    tool_version: str
    stages: dict[str, StageMeta]


# ---------------------------------------------------------------------------
# CLI wiring support: exact basename -> model map, and docs generation.
# ---------------------------------------------------------------------------

#: Exact filename -> model, in pipeline-stage order. Order matters only for
#: the rendered docs/artifacts.md (stable, readable ordering); validate-artifact
#: looks this map up by key so order there is irrelevant.
FILENAME_MODEL_MAP: dict[str, type[ArtifactModel]] = {
    "study_snapshot.json": StudySnapshot,
    "lint.json": LintReport,
    "paper_status.json": PaperStatus,
    "materials_manifest.json": MaterialsManifest,
    "paper_summary.json": PaperSummary,
    "materials_summary.json": MaterialsSummary,
    "comparison.json": Comparison,
    "fix_plan.json": FixPlan,
    "run_meta.json": RunMeta,
}

#: One-line prose purpose per artifact, for docs/artifacts.md and for
#: subagents skimming the doc instead of re-reading this whole module.
ARTIFACT_PURPOSES: dict[str, str] = {
    "study_snapshot.json": (
        "Deterministic parse of a CogGym study's config/trial/instruction "
        "files into a structured snapshot."
    ),
    "lint.json": "Structural lint findings for a study's dataset files (no LLM).",
    "paper_status.json": (
        "Status and extracted-text quality of the study's source paper PDF."
    ),
    "materials_manifest.json": (
        "Discovered and downloaded author-released materials (OSF/GitHub/"
        "other) for a study."
    ),
    "paper_summary.json": (
        "Paper-analyst's structured extraction of each paper experiment, "
        "mapped to CogGym folders."
    ),
    "materials_summary.json": (
        "Materials-analyst's verbatim evidence (instructions, randomization, "
        "response scales) mined from downloaded materials."
    ),
    "comparison.json": (
        "Field-by-field comparison of paper/materials evidence against the "
        "CogGym implementation, with discrepancies and confirmations."
    ),
    "fix_plan.json": (
        "The filtered set of clear-cut, auto-fixable discrepancies plus the "
        "PR title/body to accompany them."
    ),
    "run_meta.json": (
        "Per-stage status, input hashes, and Claude invocation metadata for "
        "one pipeline run."
    ),
}


def render_schemas_markdown() -> str:
    """Render every artifact model's JSON Schema + one-line purpose as Markdown.

    Why generated rather than hand-written: schemas.py is the single source
    of truth for every run artifact (constraints.md hard rule #2), so
    docs/artifacts.md must never drift from the pydantic models. Producing it
    mechanically via ``export-schemas`` is the only way to guarantee that.
    """
    lines = [
        "# CogGym Structure Check — Artifact Schemas",
        "",
        "Generated by `python -m coggym_check export-schemas`. Do not edit by hand.",
        "",
    ]
    for filename, model in FILENAME_MODEL_MAP.items():
        purpose = ARTIFACT_PURPOSES[filename]
        schema = json.dumps(model.model_json_schema(), indent=2, sort_keys=False)
        lines.append(f"## {model.__name__} (`{filename}`)")
        lines.append("")
        lines.append(purpose)
        lines.append("")
        lines.append("```json")
        lines.append(schema)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)
