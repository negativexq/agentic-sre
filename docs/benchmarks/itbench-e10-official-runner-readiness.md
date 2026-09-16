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

The artifact records the qualifying source commit as
`1a7134df495d064e36580795414474639c475083`; no E10
prediction manifest, ledger, seal, or official result was created.
