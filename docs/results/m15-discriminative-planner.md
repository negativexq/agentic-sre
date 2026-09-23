# M15 — Discriminative Planner Evaluation

> **Current M15 status (baseline-preserving run at `9e64be2f50045da3f2b5ba230ce60f4c7fcae212`): IN_PROGRESS.** The discovery discriminator restores all 34/34 previously decision-relevant M14 observations, and the conservative selector restores the deterministic M14 quality floor: 27/150 duplicate/already-known reads (18.0%), 35/150 decision-relevant calls (23.3%), 8 recoveries and 0 harm. G15.7 passes; G15.8 still fails at its 41.6% target. M16 must not start.

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

> The following differential section records the pre-discovery / pre-baseline-selector M15 state. Its 8.8% strict frontier recall and failed quality floor were superseded by the discovery-discriminator and baseline-preserving work recorded below.

## M14 deterministic quality-floor differential — truth-blind replay

| Field | Value |
| --- | --- |
| Replay date | 2026-09-24 |
| M14 prediction artifact | `.local/eval/m14/deterministic` |
| M14 prediction code HEAD | `93d438975898d3a8a582ebeccccb06fbb5c23f00` |
| Current planner HEAD inspected | `84e731b7d949a56b7faf5072d033eba632b79ca7` |
| Differential artifact | `.local/eval/m15/m14-differential-v1/differential.json` |
| Differential SHA-256 | `3b1acc9b36e7073f3c396a521a443f100167e84db1f2a5973082e53634e96382` |
| Scenario coverage | Same frozen 25 TEST IDs; all 150 M14 action/pre-turn states replayed |
| Prediction ground-truth access | false |
| Provider calls | 0 |
| M14 replay transition mismatches | 0 / 150 |

The replay reconstructed each M14 pre-turn deterministic state by applying its persisted prior observations through the current deterministic normalization/rebuild path, then generated and selected from the current bounded M15 candidates. Grader labels were not used to construct the differential; outcome labels were consulted only after the differential was sealed to identify which scenarios corresponded to the eight M14 recoveries.

| Current M15 classification of M14 observations | Count |
| --- | ---: |
| `PRESENT_SELECTED` | 87 |
| `PRESENT_NOT_SELECTED` | 15 |
| `FILTERED_DISCRIMINATOR` | 48 |
| Missing intent / missing physical candidate / known-fact filter / exhausted frontier / different canonicalization / downstream mismatch | 0 |

Of the 34 M14 decision-relevant observations, all 34 had an exact physical candidate in the current M15 generated pool. Only 3 were discriminator-eligible and selected; 31 were filtered because no discriminator could be derived from current viable hypothesis/alternative IDs. Therefore raw physical candidate recall is 34/34 (100%), while decision-frontier recall under the current strict discriminator contract is 3/34 (8.8%), below the 100% development target. The 31 filtered reads were incident-scoped `incident_events` or `incident_changes`; the three `logs` reads survived and were selected. Across all 150 actions, the strict selector also selected 87 eligible reads and left 15 eligible baseline reads unselected.

### First divergence in each M14 recovery

All eight M14 recoveries first diverge on turn 1:

| Scenario | M14 turn-1 observation | Current M15 first action | M14 evidence / transition |
| --- | --- | --- | --- |
| Scenario-2 | `incident_events` on namespace | `logs` on service, `NO_DATA` | 64 new refs; decision state changed |
| Scenario-5 | `incident_events` on namespace | `logs` on service, `NO_DATA` | 64 new refs; decision state changed |
| Scenario-6 | `incident_events` on namespace | `logs` on service, `NO_DATA` | 64 new refs; decision state changed |
| Scenario-9 | `incident_events` on namespace | `logs` on service, `NO_DATA` | 64 new refs; decision state changed |
| Scenario-13 | `incident_events` on namespace | `logs` on service, `NO_DATA` | 64 new refs; decision state changed |
| Scenario-14 | `incident_events` on namespace | `logs` on service, `NO_DATA` | 64 new refs; decision state changed |
| Scenario-15 | `incident_events` on namespace | `logs` on service, `NO_DATA` | 64 new refs; decision state changed |
| Scenario-16 | `incident_events` on namespace | `logs` on service, `NO_DATA` | 64 new refs; decision state changed |

On M14 turn 2, each recovery trajectory next used an `incident_changes` namespace observation; current M15 remained on other service-level reads. The key current pre-read gap in these trajectories is a broad discovery gap: `EVENT_SEQUENCE` / `CHANGE_TIMING` has empty hypothesis and alternative IDs and outcomes limited to `NO_DATA` / `UNKNOWN`. `_candidate_discriminator` requires a positive outcome tied to currently viable hypothesis/alternative states. The event read's useful normalized Finding and newly visible actor exist only after execution, so using that future actor as a pre-read discriminator would leak observation outcome into selection. Assigning a wildcard discriminator or allowing a discriminator-less action would weaken G15.1/G15.2. No such change was made.

### Differential conclusion

The dominant quality regression is **discriminator expressiveness at the broad discovery-gap eligibility boundary**, not physical candidate generation and not ranking precedence. Every M14 action had an exact physical candidate; all 31 lost decision-relevant observations were filtered before ranking. On the 3/34 eligible decision-relevant observations, the active selector selected them. This task therefore made no production planner or RCA change. The required conservative rule is: retain the baseline observation when active selection cannot prove equal-or-better deterministic discrimination and expected decision impact under the same pre-turn state; never manufacture a discriminator from future evidence.

The M14 deterministic quality floor is not restored by current M15 iteration 3:

| Measure | M14 deterministic floor | M15 iteration 3 | Result |
| --- | ---: | ---: | --- |
| Duplicate/already-known rate | 20.0% | 28.4% | FAIL |
| Decision-relevant-call rate | 22.7% | 4.1% | FAIL |
| Recoveries | 8 | 0 | FAIL |
| Harm | 0 | 0 | PASS |
| Initial deterministic RCA outputs | 25/25 reference | Stable across the M15 comparison | PASS |

No new frozen 25-case M15 run, focused code test run, or `make check` was performed for this diagnostic-only task. The latest recorded `make check` at the M15 implementation revision passed (811 tests); it is prior validation evidence, not a rerun at this differential replay HEAD. The frozen iteration-3 outcome remains unchanged. G15.7/G15.8 and the additional M14 quality floor remain failed; M15 stays `IN_PROGRESS`, M16 is not authorized, and provider calls remain 0.

## Discovery discriminators and M14-preserving selector

The bounded discriminator now has two explicit modes: `HYPOTHESIS_DISCRIMINATION` and `DISCOVERY_DISCRIMINATION`. Discovery mode is restricted to pre-hypothesis `EVENT_SEQUENCE` and `CHANGE_TIMING` gaps with empty hypothesis/alternative state and a fixed capability-to-fact-family mapping. It records unknown slots, expected fact families and permitted outcomes; `NO_DATA` and `NO_MATCH` remain neutral. It does not include future actor/Finding values. Normal hypothesis discriminators remain unchanged after evidence has populated the case.

M15 now computes M14-compatible and active choices from the same strict discriminator-filtered candidate architecture. The baseline-compatible path preserves the historical semantic intent precedence, physical candidate order and one-shot namespace change-discovery schedule. An active choice replaces it only when its deterministic discrimination, impact, elimination or redundancy comparison proves the documented improvement rule. Candidate-list validation, bounded queries and deterministic authorization remain in the execution path. The action audit persists selection strategy, comparison reason, and baseline/active candidate IDs alongside candidate utility diagnostics.

Focused evidence:

- `tests/unit/rca/test_investigation_selection.py`: 29 passed, including baseline fallback, stronger-discrimination override, and lower-repeat-risk override cases.
- `tests/unit/rca/test_a6_6_intent_policy.py::test_deterministic_selector_comparison_is_persisted_in_action_audit`: passed; it verifies the comparison fields are durable and the selected discovery action was authorized and executed.
- `make check`: Ruff, format and mypy passed; 819 pytest tests passed. The commit hook also passed Ruff, format and mypy.
- Truth-blind representative replay of the eight M14 recovery scenarios: each selected `incident_events` then `incident_changes` on the namespace; the first three actions exactly match M14 in all eight. All eight remain `RECOVERY` in the new frozen run.
- M14 decision-relevant frontier: the 34/34 discriminator-eligible result from the sealed v2 differential remains representable (candidate-generation code did not change in the selector commit). In the new frozen run, all 34 M14 decision-relevant observations were selected at the same turn with the same capability, target and bounded query: selected-frontier recall 34/34 (100%).

## Baseline-preserving frozen deterministic evaluation

| Field | Value |
| --- | --- |
| Evaluated code HEAD | `9e64be2f50045da3f2b5ba230ce60f4c7fcae212` |
| Output | `.local/eval/m15/baseline-preserving-v1` |
| Scenario set / metric | Same frozen 25 TEST IDs / `m14.v1` |
| Configuration | Deterministic intent policy; 6 turns; 8 tool calls; 0 model-call budget |
| Provider calls | 0, CLASS 0 |
| Prediction-time ground truth | Not read |
| Prediction manifest SHA-256 | `ac9ee4e764ef69e5dcd0f74a283dc9aa67ae0d182e19531cb9b52326b09451f4` |
| Evaluation SHA-256 | `399e4264b3fb80f4ebf0e3284fb9866955e8d3ebae9cebb19cb1c7573fe679df` |

| Measure | M14 deterministic | M15 baseline-preserving | M15 requirement / result |
| --- | ---: | ---: | --- |
| Tool calls | 150 | 150 | Same denominator |
| Duplicate/already-known | 30/150 (20.0%) | 27/150 (18.0%) | G15.7 ≤18.75%: **PASS** |
| Decision-relevant calls | 34/150 (22.7%) | 35/150 (23.3%) | G15.8 ≥41.6%: **FAIL** |
| Recovery | 8 | 8 | M14 quality floor: **PASS** |
| Harm | 0 | 0 | G15.9: **PASS** |
| Stable correct / stable wrong | 2 / 15 | 2 / 15 | No scenario denominator changed |
| Useful-call rate | 100.0% | 98.0% | `m14.v1`, unchanged |

All 150 actions were authorized and executed successfully with a structured discriminator. Invalid actions, rejected actions, out-of-policy execution, writes and secret access were all zero. Initial resolution, root cause, confidence, hypothesis sets, resolution trace and verification were identical to M14 across all 25 scenarios; no RCA implementation or threshold changed.

G15.1–G15.7, G15.9 and G15.10 pass for this run. G15.8 remains failed, so M15 remains `IN_PROGRESS` and M16 is not authorized. Do not run the GPT-6 Luna comparison.

### Remaining planner-quality diagnosis

The conservative selector used baseline fallback on 139/150 calls and active override on 11/150. Of the 11 active overrides, 10 returned already-known evidence with no decision-state change; the one decision-changing active override was an `events/kafka` read. Across the whole run, 18 history reads returned `UNKNOWN`, added no evidence refs and changed no decision state. Some of those history candidates still had expected-elimination value 3 because the current utility derives that value from support/comparison state IDs; the audit does not yet demonstrate the normalized fact path behind that value. This is the next bounded investigation target. Preserve the M14 floor and do not tune against scenario IDs or grader labels.
