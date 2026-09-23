# M15 — Discriminative Planner Evaluation

## Run identity

| Field | Value |
| --- | --- |
| Date | 2026-09-23 |
| Evaluated commit | `df4200abf72516502c7f02dd85780dc9c9a93c71` |
| Environment | Local repository; pinned ITBench-Lite dataset at `.local/itbench-lite` |
| Evaluation class | CLASS 0 — offline deterministic evaluation |
| Model | none |
| Config | `max_turns=6`, `max_model_calls=6` (LLM disabled), `max_tool_calls=8`, `max_tool_calls_per_gap=2` |
| Frozen set | Same 25 ITBench-Lite TEST scenario IDs as M14; prediction and grading seals verified |
| Model-call budget | 0 |
| Actual model/provider calls | 0 / 0 |
| Tool calls | 148 |
| Metric version | `m14.v1` |

Prediction manifest SHA-256: `b5d77b11e27a6d5db5bf0d2ce9d6522f25603b9d3ffadcd7959c2afc0b548dcb`.

Prediction seal SHA-256: `b8d02f23c55cfe7fa679f36a465d9a15016ca22799f14c69816f56ece5e8a6f1`.

Graded evaluation SHA-256: `ed77bf40e0caeaf8bc16521c9476f60a14cf84254269417724bb50e70a353722`.

Evaluation seal SHA-256: `dbe55b5dcdbb8f185ad7e63ce5c33d997f16e0e8f579ecf5f61797315628d129`.

## Commands

```bash
.venv/bin/agentic-sre investigation-eval \
  --split test --confirm-test \
  --out .local/eval/m15/deterministic \
  --dataset .local/itbench-lite \
  --max-turns 6 --max-model-calls 6 --max-tool-calls 8
.venv/bin/agentic-sre grade-investigation-eval \
  --out .local/eval/m15/deterministic \
  --dataset .local/itbench-lite
```

Prediction completed at the evaluated commit with `ground_truth_read_during_prediction=false`, zero model calls and zero real provider calls. The grader verified the prediction seal before loading labels. The frozen scenario IDs match the M14 deterministic comparison exactly.

## Results

| Metric | M14 GPT-6 Luna baseline | M14 deterministic comparison | M15 deterministic planner |
| --- | ---: | ---: | ---: |
| Scenarios | 25 | 25 | 25 |
| Model calls | 25 | 0 | 0 |
| Tool calls | 24 | 150 | 148 |
| Duplicate/already-known reads | 9 (37.5%) | 30 (20.0%) | 45 (30.4%) |
| Useful-call rate | 20.8% | 100.0% | 100.0% |
| Decision-relevant calls | 5 (20.8%) | 34 (22.7%) | 5 (3.4%) |
| Recovery | 4 | 8 | 0 |
| Stable correct | 2 | 2 | 2 |
| Stable wrong | 19 | 15 | 23 |
| Harm | 0 | 0 | 0 |
| AMBIGUOUS → RESOLVED | 0 | 0 | 0 |
| Invalid/rejected actions | 1/1 | 0/0 | 0/0 |
| Out-of-policy / writes / secret access | 0/0/0 | 0/0/0 | 0/0/0 |

All 148 executed M15 actions carry a structured discriminator and have `AUTHORIZED` status. The initial deterministic resolution and root actor match the M14 deterministic run for all 25 scenarios. Final investigated correctness did not hold steady: M15 ended correct in 2/25 scenarios, compared with 10/25 after M14 deterministic investigation. That recovery loss is reported separately from the unchanged initial RCA output.

M15 executed only `logs` (100 reads) and `history` (48 reads). There were 98 `NO_DATA` outcomes and 48 `UNKNOWN` outcomes; only 2 reads had a `SUPPORTS` outcome. Five of 148 calls changed decision state. The M14 deterministic comparison also used 150 calls, including 24 `incident_events` and 24 `incident_changes` reads that changed its gap/decision state. These measurements show the current discriminator filter is overly restrictive for the required impact target, while the current utility does not adequately suppress already-known/semantic duplicate reads.

## M15 hard gates

| Gate | Result | Evidence |
| --- | --- | --- |
| G15.1 Explicit discriminator for every selected read | PASS | All 148 executed actions have a structured discriminator matching the selected gap; audit validation tests pass. |
| G15.2 No blind read solely because legal | PASS | Production selectors filter to positive-discriminator candidates; a discriminator-less legal action is rejected with backend status `NOT_EXECUTED`. |
| G15.3 Semantic duplicate suppression | FAIL | Exact physical repeats are suppressed, but 45/148 calls were duplicate/already-known (30.4%); cross-read known-evidence suppression is insufficient. |
| G15.4 Known-evidence penalty | FAIL | Current candidate utility has no effective known-evidence penalty; the frozen run still records 45 duplicate/already-known calls. |
| G15.5 Deterministic planner works with LLM disabled | PASS | CLASS 0 run completed on all 25 scenarios with 0 model/provider calls. |
| G15.6 Authorization remains authoritative | PASS | 148/148 executed actions were authorized; out-of-policy executions, writes and secret access were all 0. |
| G15.7 Duplicate/already-known rate improves by at least 50% vs M14 frozen baseline | FAIL | M14 GPT-6 baseline was 37.5%; M15 was 30.4%, an 18.9% relative reduction. It is also worse than M14 deterministic's 20.0%. |
| G15.8 Decision-relevant-call rate at least doubles vs baseline | FAIL | M14 GPT-6 baseline was 20.8% (target ≥41.6%); M15 was 3.4%. M14 deterministic was 22.7%. |
| G15.9 Harm remains 0 | PASS | Grader found 0 HARM outcomes among the same frozen 25 scenarios. |
| G15.10 Deterministic RCA does not regress | PASS, initial RCA only | Initial resolution/root actor were identical to M14 in all 25 scenarios. The post-investigation recovery outcome regressed and remains a measured M15 failure outside this initial-RCA comparison. |

**Milestone result: M15 remains IN_PROGRESS.** G15.3, G15.4, G15.7 and G15.8 fail. No gate definition or scenario denominator was changed. Do not run the CLASS 2 GPT-6 Luna comparison until the deterministic gates pass.

## Validation executed

- Focused investigation/planner tests: 184 passed.
- `make check`: Ruff passed, formatting passed, mypy passed on 203 source files, pytest passed (801 passed; one existing Starlette deprecation warning).
- Commit hooks passed on the implementation commit.
- Deterministic prediction and grading completed on the same frozen 25 TEST scenarios as M14; prediction/evaluation seals verified.
- No provider call was made.

## Known limitations and next work

- The current planner uses deterministic positive-outcome metadata, but its lexicographic utility still prioritizes causal/structural relevance ahead of discrimination value.
- Exact-query duplicate suppression exists; semantic/known-evidence penalties are not yet effective.
- The M15 deterministic run failed its duplicate-rate and decision-relevance targets. Preserve this run as a failed evaluation record; any fixed implementation must be measured against the same frozen set with a new output directory. Do not rerun or overwrite the sealed prediction.
- Final accuracy/recovery fell from 10/25 correct after M14 deterministic investigation to 2/25 after M15; initial RCA outputs remained identical.
