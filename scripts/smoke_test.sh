#!/usr/bin/env bash
#
# End-to-end smoke test for the headless `run` pipeline (task 11), against
# a real study, via a real `claude` CLI subprocess per agentic stage.
#
# REQUIRES, and is NOT run by pytest (constraints.md hard rule #8: no
# network, no LLM calls in unit tests):
#   - the `claude` CLI installed and authenticated on this machine
#   - network access (paper-finder/materials-scout web search+fetch,
#     `python -m coggym_check download`'s OSF/GitHub fetches)
#   - a real Claude API budget -- every agentic stage below is a genuine
#     paid `claude -p` invocation (sonnet for most stages, opus for
#     paper-analyst/structure-comparator).
#
# Run manually from anywhere; it cds to the repo root itself:
#   ./scripts/smoke_test.sh
#
# Verifies: the pipeline runs to completion for Gerstenberg2015How, every
# artifact actually present in its run dir validates against its schema,
# report.md exists, and prints a per-stage + total cost/duration summary
# pulled from run_meta.json.

set -euo pipefail

STUDY="Gerstenberg2015How"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

RUN_DIR="runs/${STUDY}"

echo "== running headless pipeline for ${STUDY} =="
python -m coggym_check run "${STUDY}"

echo
echo "== validating every artifact present in ${RUN_DIR} =="
# paper_summary.json/materials_summary.json may legitimately be absent
# (skip rules: paper not_found/paywalled, materials none_found) -- their
# absence is not a smoke-test failure, only their presence-but-invalidity
# would be.
for artifact in \
  study_snapshot.json \
  lint.json \
  paper_status.json \
  materials_manifest.json \
  paper_summary.json \
  materials_summary.json \
  comparison.json \
  fix_plan.json \
  run_meta.json
do
  path="${RUN_DIR}/${artifact}"
  if [[ -f "${path}" ]]; then
    python -m coggym_check validate-artifact "${path}"
  else
    echo "note: ${path} not present (presumably a legitimately skipped stage)"
  fi
done

echo
echo "== asserting report.md exists =="
if [[ ! -f "${RUN_DIR}/report.md" ]]; then
  echo "FAIL: ${RUN_DIR}/report.md does not exist" >&2
  exit 1
fi
echo "ok: ${RUN_DIR}/report.md exists"

echo
echo "== cost summary (from run_meta.json) =="
python - "${RUN_DIR}/run_meta.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    run_meta = json.load(f)

total_cost = 0.0
total_duration = 0.0
print(f"{'STAGE':<20}{'STATUS':<10}{'COST_USD':<12}{'DURATION_S':<12}SESSION_ID")
for stage, meta in run_meta["stages"].items():
    claude = meta.get("claude")
    cost = claude["cost_usd"] if claude else 0.0
    duration = claude["duration_s"] if claude else 0.0
    session = claude["session_id"] if claude else "-"
    total_cost += cost
    total_duration += duration
    print(f"{stage:<20}{meta['status']:<10}{cost:<12.4f}{duration:<12.1f}{session}")
print()
print(f"TOTAL cost_usd={total_cost:.4f} duration_s={total_duration:.1f}")
PY

echo
echo "smoke test passed."
