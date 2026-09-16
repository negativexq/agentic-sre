# E10 official runner readiness

Status: `E10_OFFICIAL_RUNNER_READY_FOR_REVIEW`

This is an offline qualification artifact. No official E10 scenario was
executed.

## Architecture

The dedicated path is:

```text
typed E10 manifest
→ provider-free preflight
→ prediction checkpoints (35 × 1)
→ immutable hash seal
→ deterministic local grade after seal verification
→ official judge deferred
```

The historical `scripts/itbench_e9_execute.py` is not reused. It embeds
ground-truth grading in the prediction loop, writes historical E9 outputs, and
uses implicit `E9Limits()` defaults. The E10 path freezes those reviewed
historical values explicitly:

```text
max_model_calls = 12
max_tool_calls = 24
max_agent_turns = 12
max_wall_time_seconds = 240
max_consecutive_rejected_actions = 2
```

The ledger cap is derived as `scenario_count * max_model_calls`, so it is 420
for the pinned 35-scenario set without a separate hard-coded cap.

## Isolation and failure policy

Prediction imports no ground-truth loader or grader and has no judge path.
Preflight reports provider construction as false. Each scenario receives fresh
observable runtime state and writes seven checkpoint artifacts. A completed
checkpoint is hash-validated and skipped on resume; a partial checkpoint,
identity mismatch, or infrastructure failure fails closed. Normal model/control
terminals are persisted and the runner continues to the next scenario.

The prediction seal requires all 35 checkpoints and rechecks their hashes.
`grade-local` verifies that seal before loading ground truth and reports zero
provider calls. The official judge remains
`DEFERRED_UNTIL_PREDICTIONS_FROZEN_AND_HUMAN_AUTHORIZED`.

## Offline proof

The FakeModelProvider qualification completed all 35 scenarios in canonical
order with one trial, persisted and reloaded the checkpoint artifacts, resumed
without issuing new provider requests, created and verified the seal, and then
performed provider-free local grading. Ground truth was not called during
prediction qualification.

SMOKE-005 is pinned as a prerequisite with `LIVE_SMOKE_PASS` and
`V5_CONTRACT_LIVE_GATE = PASS`; its artifacts remain immutable.

## Network accounting

```text
live agent calls: 0
OpenAI outbound attempts: 0
Luna calls: 0
judge calls: 0
official benchmark scenarios: 0
live smoke scenarios: 0
ground-truth accesses during prediction qualification: 0
```

## Final integrity guards

Resume accounting is delta-based. A continuation captures the ledger and
provider snapshots before pending scenarios, then requires:

```text
(budget_after.calls_used - budget_before.calls_used)
==
(provider_after.outbound_api_attempts - provider_before.outbound_api_attempts)
```

Capacity is checked only for pending scenarios, while prior ledger consumption
remains monotonic. The offline regression covered five sealed scenarios with
five prior calls and thirty pending scenarios, producing thirty resumed
provider attempts and a final ledger of 35 calls.

`packages/provider/budget.py` is part of `E10_OFFICIAL_RELEVANT_PATHS`, so a
change to the shared paid-call accounting implementation invalidates E10
identity. Before a checkpoint is accepted, the measured runtime safety fields
`ground_truth_exposure`, `cross_scenario_evidence`, `writes`, and
`arbitrary_execution` must all be zero. The only continuable terminals are:

```text
SUBMIT, STOP, MODEL_STEP_LIMIT, PROTOCOL_STALLED, WALL_TIME_LIMIT
```

`PROVIDER_ERROR` and unknown terminals persist failure evidence, mark the
ledger failed, and abort prediction generation.

The qualifying source patch is `0d5c5ec`; no E10 prediction manifest, ledger,
seal, official result, or live provider execution was created.
