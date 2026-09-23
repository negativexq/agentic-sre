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

- The current planner uses deterministic positive-outcome metadata. The known-evidence and semantic-repeat penalties added at `556088f` did not affect any selected action in the frozen run: the action sequence is identical to the preceding M15 run across all 25 scenarios and 148 actions. This indicates the penalty fields are not influencing the effective selection frontier for the repeated reads.
- Exact-query duplicate suppression exists; semantic/known-evidence penalties remain ineffective at reducing the measured duplicate rate.
- The M15 deterministic run failed its duplicate-rate and decision-relevance targets. Preserve both sealed runs; any later fix must use the same frozen set with a new output directory. Do not overwrite either prediction.
- Final accuracy/recovery fell from 10/25 correct after M14 deterministic investigation to 2/25 after M15; initial RCA outputs remained identical.

## M15 deterministic ranking iteration 2

| Field | Value |
| --- | --- |
| Date | 2026-09-23 |
| Evaluated commit | `556088fbfcf8d2f40ab124e2c5270eff9e67a52c` |
| Output | `.local/eval/m15/known-risk-v2` |
| Evaluation class | CLASS 0 — offline deterministic evaluation |
| Model calls / real provider calls | 0 / 0 |
| Scenario set | Same frozen 25 ITBench-Lite TEST IDs as M14 and the first M15 run |
| Configuration | `max_turns=6`, `max_model_calls=6` (LLM disabled), `max_tool_calls=8`, `max_tool_calls_per_gap=2` |
| Metric version | `m14.v1` |
| Prediction manifest SHA-256 | `70ac31f4ffc08c381b73233214d1e15f35db8b68df01652c9d3eebab768f4fd8` |
| Prediction seal SHA-256 | `ac84cbabb21ec67b41fe29555bda200ca1ab2e1171bc0905bf0167bc1337497d` |
| Graded evaluation SHA-256 | `979be35642577b563f3a4c3dd5499ab5d90da14731066300c6dc359630c4d3ca` |

Commands:

```bash
.venv/bin/agentic-sre investigation-eval \
  --split test --confirm-test \
  --out .local/eval/m15/known-risk-v2 \
  --dataset .local/itbench-lite \
  --max-turns 6 --max-model-calls 6 --max-tool-calls 8
.venv/bin/agentic-sre grade-investigation-eval \
  --out .local/eval/m15/known-risk-v2 \
  --dataset .local/itbench-lite
```

| Metric | First M15 run | Ranking iteration 2 | M15 gate |
| --- | ---: | ---: | --- |
| Scenarios | 25 | 25 | frozen set |
| Tool calls | 148 | 148 | — |
| Duplicate/already-known reads | 45 (30.4%) | 45 (30.4%) | ≤18.75% vs 37.5% M14 GPT-6 baseline |
| Useful-call rate | 100.0% | 100.0% | report only |
| Decision-relevant calls | 5 (3.4%) | 5 (3.4%) | ≥41.6% vs 20.8% M14 GPT-6 baseline |
| Recovery | 0 | 0 | report only in M15 |
| Harm | 0 | 0 | 0 |
| Model/provider calls | 0/0 | 0/0 | 0 for CLASS 0 |

All 148 selected action identities (capability, target, query and gap) exactly match the first M15 run. The deterministic initial diagnoses and final outcome counts also match: `RECOVERY=0`, `STABLE_CORRECT=2`, `STABLE_WRONG=23`, `HARM=0`. G15.3, G15.4, G15.7 and G15.8 remain FAIL; the other previously passing gates remain unchanged. No gate, metric, scenario ID or denominator changed. M15 remains `IN_PROGRESS`; no GPT-6 Luna comparison is authorized or performed.

Focused diagnosis from the run: the penalties are calculated in physical candidate and intent bundle utilities, but the active ranking path still selects the exact same reads. The next task is to trace the effective per-turn candidate/bundle frontier and make the risk penalty participate at the actual selection boundary, with a regression test that proves a lower-risk discriminative candidate wins when both are offered. Keep the frozen 25-scenario evaluation sealed and unchanged.
