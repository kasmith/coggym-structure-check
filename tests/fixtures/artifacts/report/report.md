# Structure Check Report: TestStudy2020Mini

- **Study:** TestStudy2020Mini
- **Commit:** 1a2b3c4d5e6f7890abcdef1234567890abcdef12
- **Generated:** 2026-01-15T12:00:00Z
- **Tool version:** 0.1.0

## Coverage

- **Paper evidence:** full
- **Materials evidence:** partial

> **Coverage caveat:** materials evidence is incomplete for this study — treat every discrepancy below (and every confirmation) as correspondingly less certain.

### Paper status

- **Status:** found_local
- **PDF path:** studies/TestStudy2020Mini/paper.pdf
- **Pages:** 10
- **Text quality:** 0.94
- **Needs direct PDF read:** False

### Materials manifest

- **Status:** partial
- **Sources found:** 2
- **Sources downloaded ok:** 1/2
- **Notes:** github source failed: repository appears to have been deleted

## Fix plan

- **Fixes applied:** 1
- **Fixes declined:** 1

### Declined fixes

- **d4**: DOI correction touches citation metadata read by external indexers; held back for a human to confirm the paper's printed DOI resolves before changing it

## Experiments

### exp1

#### Structure summary

| Metric | Value | Evidence |
| --- | --- | --- |
| Conditions | 1 | paper p.4 |
| Trials | 3 | paper p.4 |
| Instructions | 2 | coggym config.json |
| Response type(s) | ['multi-slider'] | paper p.5 |

#### Confirmations

| Field | Value | Evidence ref |
| --- | --- | --- |
| n_conditions | 1 | paper p.4 |
| n_trials | 3 | paper p.4 |
| n_instructions | 2 | coggym config.json |
| response_types | ['multi-slider'] | paper p.5 |

#### Discrepancies — Auto-fixed

| ID | Category | Severity | Field | Paper value | CogGym value | Rationale |
| --- | --- | --- | --- | --- | --- | --- |
| d1 | instruction_text | minor | instruction_texts.instruction_intro | Participants were welcomed and told they would watch three scenarios. | Welcome to the study. You will watch three short scenarios. | coggym text should match verbatim materials wording ("clips"), not the paraphrase currently in place ("scenarios") |

#### Discrepancies — Needs human judgment

| ID | Category | Severity | Field | Paper value | CogGym value | Rationale |
| --- | --- | --- | --- | --- | --- | --- |
| d2 | randomization | major | experimentFlow.0.block_randomization.1 | — | False | materials code shuffles the trial array, but it is unclear whether this ran in the deployed version or only a pilot build |

### exp2

#### Structure summary

| Metric | Value | Evidence |
| --- | --- | --- |
| Conditions | 1 | paper p.6 |
| Trials | see discrepancies | — |
| Instructions | see discrepancies | — |
| Response type(s) | see discrepancies | — |

#### Confirmations

| Field | Value | Evidence ref |
| --- | --- | --- |
| n_conditions | 1 | paper p.6 |

#### Discrepancies — Auto-fixed

| ID | Category | Severity | Field | Paper value | CogGym value | Rationale |
| --- | --- | --- | --- | --- | --- | --- |
| d4 | citation | minor | citation.paperDOI | 10.1000/test-correct | 10.1000/test | paperDOI does not match the DOI printed on the paper itself |

#### Discrepancies — Needs human judgment

| ID | Category | Severity | Field | Paper value | CogGym value | Rationale |
| --- | --- | --- | --- | --- | --- | --- |
| d3 | response_scale | critical | queries_by_tag.causal_rating.slider.labels | scale anchored 'definitely not the cause' to 'definitely the cause' | [{'value': 1, 'label': 'not at all'}, {'value': 7, 'label': 'very much'}] | no materials evidence to confirm exact anchor wording, so this is not clear-cut enough to auto-fix despite the paper quote |

## Evidence appendix

### exp1

**d1** (instruction_text, minor)

- [materials] "Welcome to the study. You will watch three short clips." (materials/osf/abc12/instructions.txt:1)

**d2** (randomization, major)

- [materials] "trials = shuffle(trials);" (materials/osf/abc12/exp.js:14)

### exp2

**d3** (response_scale, critical)

- [paper] "the scale ran from "definitely not the cause" to "definitely the cause"" (p.6 §Method)

**d4** (citation, minor)

- [paper] "https://doi.org/10.1000/test-correct" (p.1 header)
