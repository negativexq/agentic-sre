# M15 — Discriminative Planner Evaluation

> **Current M15 status (iteration 3, evaluated at `16296d516b0957c086de82db37be08e15446b399`): IN_PROGRESS.** The effective deterministic ranking is now lexicographic and every executed action records its offered candidate/risk trace. The same frozen 25-scenario CLASS 0 run measured 42/148 duplicate/already-known reads (28.4%) and 6/148 decision-relevant calls (4.1%). Harm stayed at 0. G15.7 and G15.8 still fail; M16 must not start.

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

## M15 hard gates — initial deterministic run (historical iteration 1)

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

- At `556088f`, known-evidence and semantic-repeat fields were behind static intent priorities and all 148 action identities repeated. Iteration 3 moves active discrimination and expected elimination to the front, then duplicate/NO_DATA risk, known facts, frontier, cost and stable identity. It also records the actual ranked intent/candidate rows in each action audit.
- Iteration 3 changed the sequence only for Scenario-38 among the four diagnostic representatives and only Scenario-38 across the full frozen 25 when compared with iteration 2. This is consistent with the audit: Scenario-22 and Scenario-6 offered tied history candidates with the same known-risk vector; Scenario-102 offered logs with substantially stronger discrimination; Scenario-38 offered an events read with higher elimination potential.
- The deterministic planner still misses both the duplicate-rate and decision-relevance gates. In the frozen iteration-3 audit, selected semantic-duplicate risk was 0 for all 148 calls, while known-evidence risk was nonzero for 49 calls. No same-intent candidate with equal discrimination/elimination and strictly lower duplicate/known risk was offered on a selected turn. This points to a remaining candidate/evidence-identity or candidate-frontier limitation, not an ordering-key bypass.
- Useful-call rate is 98.0% while decision-relevant-call rate is 4.1%. Do not redefine `m14.v1` in M15; report this metric limitation without changing its gate.
- Preserve all three sealed frozen runs and the representative probes. Do not overwrite earlier predictions, tune on scenario labels, or change the test population.
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

Iteration-2 diagnosis: penalties were calculated in physical candidate and intent bundle utilities, but the effective ranking path selected the exact same reads. Task 6 addressed the ordering boundary; iteration-3 results and the remaining evidence-risk analysis follow below. All earlier frozen runs remain preserved.

## M15 deterministic active-diagnosis ranking — iteration 3

| Field | Value |
| --- | --- |
| Date | 2026-09-23 |
| Evaluated commit | `16296d516b0957c086de82db37be08e15446b399` |
| Output | `.local/eval/m15/active-diagnosis-v3` |
| Evaluation class | CLASS 0 — offline deterministic evaluation |
| Scenario set | Same frozen 25 ITBench-Lite TEST IDs as M14 and earlier M15 runs |
| Metric version | `m14.v1` |
| Configuration | `max_turns=6`, `max_model_calls=6` (LLM disabled), `max_tool_calls=8`, `max_tool_calls_per_gap=2` |
| Model calls / real provider calls | 0 / 0 |
| Tool calls | 148 |
| Prediction manifest SHA-256 | `f1ccf28dd6c4043fabff48132715bb61f3f77298151f8fe588b42f65896164e6` |
| Prediction seal SHA-256 | `07dd1585c43fb3847502e2ea5da6d0081794dda1d57e6c146cdb4e8c1f042690` |
| Graded evaluation SHA-256 | `4ca283796aef8a98a9fda0fa58f3056f0edb280a319ab0a432c2a9eb3b20ffc0` |
| Evaluation seal SHA-256 | `00b6285434eb9c81b6c046342d5f5fc8414d2976a291b1951b5a28ca1cf6651b` |

Commands:

```bash
.venv/bin/agentic-sre investigation-eval \
  --split test --confirm-test \
  --out .local/eval/m15/active-diagnosis-v3 \
  --dataset .local/itbench-lite \
  --max-turns 6 --max-model-calls 6 --max-tool-calls 8
.venv/bin/agentic-sre grade-investigation-eval \
  --out .local/eval/m15/active-diagnosis-v3 \
  --dataset .local/itbench-lite
```

Prediction manifest records `git_dirty=false`, `ground_truth_read_during_prediction=false`, the evaluated commit SHA, fixed TEST IDs, zero model budget and zero provider calls. Grading completed only after the prediction seal was present and verified.

| Metric | M15 iteration 2 | M15 iteration 3 | Gate/interpretation |
| --- | ---: | ---: | --- |
| Scenarios | 25 | 25 | Frozen unchanged |
| Tool calls | 148 | 148 | — |
| Duplicate/already-known reads | 45 (30.4%) | 42 (28.4%) | Improvement of 24.3% relative to the 37.5% M14 GPT-6 baseline; target ≤18.75%, FAIL |
| Useful-call rate | 100.0% | 98.0% | `m14.v1`, unchanged definition |
| Decision-relevant calls | 5 (3.4%) | 6 (4.1%) | Target ≥41.6%, FAIL |
| Recovery / stable correct / stable wrong | 0 / 2 / 23 | 0 / 2 / 23 | No recovery improvement |
| Harm | 0 | 0 | PASS |
| NO_DATA observations | 98 | 97 | — |
| Invalid/rejected / out-of-policy / writes / secrets | 0 / 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 | PASS |
| Model/provider calls | 0 / 0 | 0 / 0 | CLASS 0 |

The iteration-3 run recorded 1,481 bounded candidate comparison rows across 148 executed actions. All executed actions had a discriminator, were authorized, and retained exactly one selected candidate in the audit. Compared with iteration 2, the full 25-case selected action sequence changed only for Scenario-38. Its second action changed from `logs/frontend-proxy` to `events/kafka`; candidate diagnostics show stronger deterministic elimination potential for that observation. In selected rows, semantic-duplicate risk was 0 on all 148 actions; known-evidence risk was nonzero on 49. No offered candidate within the selected intent had equal discrimination/elimination and strictly lower known/duplicate risk than the selected candidate.

Initial resolution, root actor, leading hypothesis, alternative hypotheses, ambiguous hypotheses, resolution trace and verification were identical in all 25 cases versus iteration 2. Scenario-38 had differences in non-leading `Diagnosis.alternatives` causal-path metadata; the same field varied across the three repeated local predictions of Scenario-38, so this is recorded as a repository determinism limitation, not attributed to this planner patch. No RCA/resolution implementation was changed.

### Iteration-3 hard-gate assessment

| Gate | Result | Evidence |
| --- | --- | --- |
| G15.1 Explicit discriminator for every selected read | PASS | 148/148 executed actions have a structured discriminator. |
| G15.2 No blind read merely because legal | PASS | Strict discriminator-filtered candidate selection remains; focused tests cover non-discriminative exclusion. |
| G15.3 Semantic duplicate suppression | PASS (mechanism) | Semantic fingerprints include capability, canonical target, dimension, discriminator states and time-scope class; equivalent repeat/NO_DATA retry tests rank below equally discriminative alternatives. The aggregate duplicate-rate target is separately G15.7 and still fails. |
| G15.4 Known-evidence penalty | PASS (mechanism) | Normalized semantic fact keys are ranked below equivalent fresh candidates; selected-call audit exposes known-evidence risk. G15.7 still fails. |
| G15.5 Deterministic planner works with LLM disabled | PASS | Complete frozen run: 0 model/provider calls. |
| G15.6 Authorization remains authoritative | PASS | 148/148 executed actions authorized; no rejected action executed; safety counts all 0. |
| G15.7 Duplicate/already-known rate improves ≥50% relative to M14 GPT-6 baseline | FAIL | 28.4% vs ≤18.75% target; relative reduction from 37.5% is 24.3%. |
| G15.8 Decision-relevant-call rate improves ≥2× M14 GPT-6 baseline | FAIL | 4.1% vs ≥41.6% target. |
| G15.9 Harm remains 0 on frozen set | PASS | 0 HARM. |
| G15.10 Deterministic RCA does not regress | PASS, with disclosed variance | Primary resolution/root/hypothesis and resolution traces match in 25/25; non-leading structural alternative path metadata differs in Scenario-38 and is nondeterministic across repeated prediction processes. |

**Milestone result: M15 remains IN_PROGRESS.** Numeric gates G15.7 and G15.8 fail. Do not start M16 or run the GPT-6 Luna comparison. No gate, metric definition, scenario ID, denominator or truth boundary changed.
