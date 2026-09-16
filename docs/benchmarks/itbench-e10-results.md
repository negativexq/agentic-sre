# ITBench E10 results

This document is derived from the frozen E10 manifest, prediction seal,
per-scenario trial manifests, and provider-free local grading result. It is
not a tuning or rerun report.

## Execution identity

```text
execution: ITB-E10
experiment: itbench-lite-sre-external-eval-v10
runtime source SHA: 82cb3ce13d2e6995008bee981b99b27f7d7ac2f9
runtime bundle SHA: fcce066898d49ad64633dee7b90a90af539dd967e35f2496780dcca1e59505f8
manifest SHA256: 4c1f21636b6d9fa7016065429049ec5a92fb90823e7a7a9a7542a39d58e94b93
dataset revision: d0916b08ba421ce5e672e9ad68aa947d938dfef0
scenario count: 35
trial count: 1
scenario order: canonical ITBENCH_SCENARIO_IDS
```

Model policy was frozen as `openai / gpt-5.6-luna / none` with zero provider
retries. The runtime envelope was 12 model calls, 24 semantic tool calls, 12
agent turns, 240 seconds, and two consecutive rejected actions per scenario.

## Prediction execution

```text
predictions completed: 35 / 35
provider invocations: 405
outbound attempts: 405
successful model responses: 405
input tokens: 1,133,576
output tokens: 25,024
semantic actions: 273
action rejections: 0
recovered rejections: 0
evidence items: 273
ledger: 405 / 420 consumed
ledger remaining: 15
ledger status: COMPLETE
prediction wall time: 2,298,908 ms
provider latency total: 921,658 ms
calls per scenario: min 6, median 12, max 12
```

Terminal distribution:

```text
SUBMIT: 16
STOP: 19
MODEL_STEP_LIMIT: 0
PROTOCOL_STALLED: 0
WALL_TIME_LIMIT: 0
```

Recorded context sizes across the 405 turns were min 6,032, median 9,242,
and max 13,811 characters.

## Prediction seal

```text
seal completion count: 35
seal verification: PASS
seal SHA256: c212764cc5ac8b22d5a6a93c3ff79eb28c074c4610864ef1a9c06c1a11bbfda4
```

All `agent_output.json`, `native_artifact.json`, and `trial_manifest.json`
hashes verified before grading. The prediction phase did not load ground
truth or invoke a judge. Local grading was run only after this verified seal
and made zero provider calls.

## Local fixed-35 grading

```text
macro precision: 0.0
macro recall: 0.0
macro F1: 0.0

micro true positives: 0
micro predicted: 16
micro ground truth: 35
micro precision: 0.0
micro recall: 0.0
micro F1: 0.0

diagnosis coverage: 0.45714285714285713 (16 / 35)
scenarios with F1 > 0: 0
scenarios with F1 = 0: 35
scenarios with at least one true positive: 0
```

## Per-scenario results

| Scenario | Terminal | Calls | Semantic actions | TP | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Scenario-1 | SUBMIT | 11 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-2 | STOP | 12 | 7 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-4 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-5 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-6 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-7 | SUBMIT | 11 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-8 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-9 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-11 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-12 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-13 | SUBMIT | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-14 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-15 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-16 | SUBMIT | 10 | 7 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-17 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-18 | STOP | 12 | 7 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-19 | SUBMIT | 6 | 4 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-20 | SUBMIT | 11 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-21 | SUBMIT | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-22 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-23 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-24 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-25 | SUBMIT | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-29 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-31 | SUBMIT | 11 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-33 | SUBMIT | 11 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-34 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-35 | STOP | 12 | 7 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-38 | SUBMIT | 12 | 9 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-80 | SUBMIT | 11 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-81 | SUBMIT | 11 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-83 | SUBMIT | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-91 | SUBMIT | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-102 | SUBMIT | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-105 | STOP | 12 | 8 | 0 | 0.000 | 0.000 | 0.000 |

Descriptive outcome categories are 19 no-diagnosis `STOP` outcomes and 16
submitted outcomes with zero true-positive matches. There were no step-limit,
protocol-limit, partial-match, or positive-match outcomes.

## Safety and deferred evaluation

Aggregate measured safety counters were all zero:

```text
ground_truth_exposure: 0
cross_scenario_evidence: 0
writes: 0
arbitrary_execution: 0
```

```text
ground truth during prediction: 0
ground truth access: post-seal local grading only
official judge calls: 0
official judge: DEFERRED_UNTIL_PREDICTIONS_FROZEN_AND_HUMAN_AUTHORIZED
```

Historical chronology remains visible: SMOKE-001 preflight failure, SMOKE-002
opaque provider failure, SMOKE-003 root-schema failure, SMOKE-004 live pass
with local rationale drift, SMOKE-005 contract pass, then this frozen E10
35-by-1 prediction and local grading result.

This result measures the frozen system’s single E10 execution and local
deterministic grading only. It does not establish RCA quality beyond these
observed metrics, and no model-based judge or additional trial was run.
