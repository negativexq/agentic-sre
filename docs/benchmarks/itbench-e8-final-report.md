# ITBench E8 final report

## A. Repair verification

| Issue | Status |
|---|---|
| Smoke worst-case budget | FIXED |
| Alert digest information loss | FIXED |
| Candidate rejected reactivation | FIXED |
| Active candidate max bypass | FIXED |
| Evidence dict packing | FIXED |
| Logs service filter | FIXED |
| Alert service aliases | FIXED |
| Event reason/type filters | FIXED |
| Citation accounting | FIXED |
| Telemetry performance | PARTIAL: implicit entity-context metric scan removed; dedicated scans remain measurable costs |
| Public argument parity | FIXED by registry/tool tests |
| Judge checkpointing | FIXED |

Smoke-001 and Smoke-002 remain immutable. Smoke-002 was a pre-official
readiness/budget defect, not a model-quality result. E8 official execution
began only after Smoke-003 had the full five-call capacity and reached a valid
terminal.

## B. Runtime identity

| Field | Value |
|---|---|
| Execution | ITB-E8 |
| Experiment | itbench-lite-sre-external-eval-v8 |
| Runtime SHA | `e474db9432b17fc854873fa8458a3b9b0de8d49a` |
| Dataset | `d0916b08ba421ce5e672e9ad68aa947d938dfef0` |
| SRE | `v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7` |
| Prompt | `itbench_sre_investigator_v6` |
| Protocol | `itbench_investigation_decision_v4` |
| Context | `itbench_external_context_v3` |
| Model | `gpt-5.6-luna` |
| Reasoning | `none` |
| Limits | 5 model calls, 12 semantic executions, 5 turns, 180s, 0 retries |
| Manifest SHA | `2736d205bf7cfa0be04f52ae16e9b1359efad372c2823906401071f4e9d75e42` |

## C. Smoke-003

`ITB-E8-SMOKE-003` passed. It used 5/5 smoke calls, 12 semantic requests,
11 executions, 11 evidence handles, five turns, and ended with `STOP` after
real provider/tool/evidence activity. Artifact reload and ledger reconciliation
passed; GT exposure and writes were zero. Artifact:
[itbench-e8-live-smoke-003.json](itbench-e8-live-smoke-003.json).

## D. Offline qualification

35/35 zero-network snapshot qualification passed. OpenAI calls and judge calls
were zero during qualification; runtime GT access was false. Full pytest,
Ruff, mypy, `make check`, `make a1-eval-check`, and CI passed before official
execution.

Context qualification: initial totals min/median/p95/max were
9,923/10,247/10,504/10,590 chars. Scenario-105 was 10,590 chars versus E7's
approximately 65,135 chars. The repaired alert digest was 1,567 chars rather
than the previous information-loss result of 18 chars.

## E. Official 35×1 results

| Scenario | Terminal | Calls | Input tokens | Tool req. | Tool exec. | Predicted entities | TP | Local F1 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Scenario-1 | MODEL_DECISION_INVALID | 1 | 6076 | 0 | 0 | 0 | 0 | 0 |
| Scenario-2 | SUBMIT_DIAGNOSIS | 5 | 33773 | 12 | 12 | 2 | 0 | 0 |
| Scenario-4 | SUBMIT_DIAGNOSIS | 5 | 32562 | 12 | 12 | 1 | 0 | 0 |
| Scenario-5 | MODEL_DECISION_INVALID | 5 | 28863 | 12 | 12 | 0 | 0 | 0 |
| Scenario-6 | MODEL_DECISION_INVALID | 1 | 6052 | 0 | 0 | 0 | 0 | 0 |
| Scenario-7 | MODEL_DECISION_INVALID | 4 | 25643 | 9 | 9 | 0 | 0 | 0 |
| Scenario-8 | MODEL_DECISION_INVALID | 4 | 26207 | 9 | 9 | 0 | 0 | 0 |
| Scenario-9 | SUBMIT_DIAGNOSIS | 5 | 33718 | 12 | 12 | 2 | 0 | 0 |
| Scenario-11 | MODEL_DECISION_INVALID | 5 | 32981 | 12 | 11 | 0 | 0 | 0 |
| Scenario-12 | MODEL_DECISION_INVALID | 5 | 29952 | 12 | 12 | 0 | 0 | 0 |
| Scenario-13 | MODEL_DECISION_INVALID | 2 | 11856 | 3 | 3 | 0 | 0 | 0 |
| Scenario-14 | MODEL_DECISION_INVALID | 5 | 31337 | 12 | 12 | 0 | 0 | 0 |
| Scenario-15 | MODEL_DECISION_INVALID | 2 | 12426 | 3 | 3 | 0 | 0 | 0 |
| Scenario-16 | MODEL_DECISION_INVALID | 1 | 6140 | 0 | 0 | 0 | 0 | 0 |
| Scenario-17 | MODEL_DECISION_INVALID | 4 | 24881 | 9 | 9 | 0 | 0 | 0 |
| Scenario-18 | STOP | 5 | 28818 | 12 | 12 | 0 | 0 | 0 |
| Scenario-19 | SUBMIT_DIAGNOSIS | 5 | 31037 | 12 | 11 | 1 | 0 | 0 |
| Scenario-20 | SUBMIT_DIAGNOSIS | 5 | 29556 | 12 | 12 | 1 | 0 | 0 |
| Scenario-21 | MODEL_DECISION_INVALID | 4 | 28830 | 9 | 8 | 0 | 0 | 0 |
| Scenario-22 | STOP | 5 | 34686 | 12 | 12 | 0 | 0 | 0 |
| Scenario-23 | SUBMIT_DIAGNOSIS | 5 | 33502 | 11 | 11 | 1 | 0 | 0 |
| Scenario-24 | SUBMIT_DIAGNOSIS | 5 | 34010 | 12 | 11 | 1 | 0 | 0 |
| Scenario-25 | STOP | 5 | 33485 | 12 | 12 | 0 | 0 | 0 |
| Scenario-29 | MODEL_DECISION_INVALID | 4 | 27332 | 9 | 9 | 0 | 0 | 0 |
| Scenario-31 | MODEL_DECISION_INVALID | 5 | 31377 | 12 | 12 | 0 | 0 | 0 |
| Scenario-33 | MODEL_DECISION_INVALID | 3 | 19248 | 6 | 6 | 0 | 0 | 0 |
| Scenario-34 | STOP | 5 | 30235 | 12 | 12 | 0 | 0 | 0 |
| Scenario-35 | MODEL_DECISION_INVALID | 5 | 31221 | 12 | 12 | 0 | 0 | 0 |
| Scenario-38 | MODEL_DECISION_INVALID | 3 | 17997 | 6 | 6 | 0 | 0 | 0 |
| Scenario-80 | STOP | 5 | 32681 | 12 | 12 | 0 | 0 | 0 |
| Scenario-81 | STOP | 5 | 30378 | 12 | 12 | 0 | 0 | 0 |
| Scenario-83 | MODEL_DECISION_INVALID | 5 | 27721 | 12 | 12 | 0 | 0 | 0 |
| Scenario-91 | MODEL_DECISION_INVALID | 5 | 32702 | 12 | 12 | 0 | 0 | 0 |
| Scenario-102 | MODEL_DECISION_INVALID | 2 | 12046 | 3 | 3 | 0 | 0 | 0 |
| Scenario-105 | STOP | 5 | 29246 | 12 | 12 | 0 | 0 | 0 |

Totals: 145 model calls, 918,575 input tokens, 48,170 output tokens, 329
semantic requests, 325 executions, 1,045.012 seconds wall time, and no local
true positives.

Reliability: `SUBMIT_DIAGNOSIS=7`, `STOP=7`,
`MODEL_DECISION_INVALID=21`, `TOOL_CALL_LIMIT=0`, `MODEL_CALL_LIMIT=0`,
`WALL_TIME_LIMIT=0`. Diagnosis coverage is 7/35 = 20.00%.

## F. Official Luna quality

`ROOT_CAUSE_ENTITY mean F1 = 0.02857142857142857`, denominator 35. The only
non-zero judge result was Scenario-20 with F1 1.0. Judge identity:

- provider: OpenAI
- model: `gpt-5.6-luna`
- evaluator: `14f026fc9cc348c4ecec5ab32714de954c95c1b1`
- compatibility: `itbench_luna_judge_compat_v1`
- temperature: 1
- provider retries: 0
- attempts per case: 1
- calls: 35/35, checkpointed and aggregated offline

## G. E7 → E8 quality and efficiency

| Metric | E7 | E8 | Delta |
|---|---:|---:|---:|
| Luna official RCA F1 | 0.171717 | 0.028571 | -0.143146 |
| Local fixed-35 macro F1 | 0.085714 | 0.000000 | -0.085714 |
| Diagnosis coverage | 17/35 | 7/35 | -10 cases |
| MODEL_DECISION_INVALID | 13 | 21 | +8 |
| Agent calls | 171 | 145 | -26 |
| Calls/scenario | 4.89 | 4.14 | -15.2% |
| Input tokens | ~2,244,100 | 918,575 | -59.1% |
| Tool requests | 385 | 329 | -14.5% |
| Tool executions | 369 | 325 | -11.9% |
| Timeouts | 0 | 0 | unchanged |

E8 is therefore `QUALITY_DOWN_EFFICIENCY_UP`, not Pareto-improved. The
official F1 delta is `(0.02857142857142857 - 0.1717171717171717) =
-0.14314574314574313`, approximately -83.36% relative. This comparison uses
the same Luna judge identity and disclosed denominator.

## H. Backend and safety

Zero-network backend qualification reported entity-context median/p95/max
latency of 1.12/1.48/1.48s, metrics 8.68/13.40/14.04s, logs
2.13/2.81/5.29s, and traces 2.40/4.83/5.00s. Snapshot instrumentation
reported source scans, records scanned, cache hits, cache misses, and entity
index lookups; live aggregate backend counters were not durably emitted in the
final result aggregate.

Safety counters: GT exposure 0, cross-scenario evidence 0, fabricated evidence
accepted 0, writes/remediation 0, shell 0, arbitrary filesystem access 0, SQL
0, arbitrary PromQL 0.

## I. Accounting and validity

Agent ledger: Smoke-003 5/5; official E8 145/175. Judge ledger: 35/35,
separate from agent accounting. The historical Smoke-001/002 accounting is
preserved and not refunded. No historical E4/E5/E6/E7 prediction was changed.

The official E8 execution was exactly one ordered 35×1 run. It is not an
untouched held-out generalization result. No E9 was started.

## J. Final classification

`ITB_E8_COMPLETE_EFFICIENCY_IMPROVED_QUALITY_REGRESSED`

`QUALITY_DOWN_EFFICIENCY_UP`

READY_FOR_ITBENCH_E8_FINAL_RESULT_REVIEW
