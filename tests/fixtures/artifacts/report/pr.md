# Fix structure discrepancies in TestStudy2020Mini

This PR applies 1 automatic fix to `TestStudy2020Mini` and flags 2 discrepancies for human review.

## Auto-fixed

- **instruction_text** (instruction text corrected to match verbatim materials): d1

## Declined fixes

- **d4**: DOI correction touches citation metadata read by external indexers; held back for a human to confirm the paper's printed DOI resolves before changing it

## Needs human judgment

- [ ] **d2** (major, randomization): materials code shuffles the trial array, but it is unclear whether this ran in the deployed version or only a pilot build
- [ ] **d3** (critical, response_scale): no materials evidence to confirm exact anchor wording, so this is not clear-cut enough to auto-fix despite the paper quote

🤖 Generated with coggym-structure-check (review before submitting)
