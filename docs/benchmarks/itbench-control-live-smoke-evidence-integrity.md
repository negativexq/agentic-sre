# ITB control live-smoke evidence integrity

## Scope

This is an offline repair of the future live-smoke executor. No provider,
OpenAI transport, Luna call, judge, live-smoke scenario, or official benchmark
scenario was run. Historical SMOKE-001 remains unchanged and immutable.

## Historical SMOKE-001

SMOKE-001 remains a fail-closed preflight attempt, not a live model trial:

| Field | Value |
|---|---|
| Classification | `LIVE_SMOKE_PREFLIGHT_FAILED` |
| Provider constructed | No |
| Agent calls | 0 |
| OpenAI outbound attempts | 0 |
| Luna calls | 0 |
| Judge calls | 0 |
| Live-smoke scenarios | 0 |
| Official benchmark scenarios | 0 |
| Ledger | cap 8, used 0, remaining 8, `NOT_STARTED` |

The four SMOKE-001 files were not edited.

## Confirmed failure mechanism and repair

The pre-patch `execute_live_smoke` caught every post-start exception, marked the
ledger `FAILED`, and re-raised. It did not guarantee a terminal summary at
`result_path`. It also raised on non-success runtime terminals before publishing
the old success summary and represented smoke safety with literal values.

The repaired flow is:

```text
manifest → provider-free preflight → fresh budget → RUNNING ledger
         → provider/runtime → native artifacts → strict reload/replay
         → terminal/accounting/safety checks
         → one atomic terminal summary → COMPLETE or FAILED ledger
```

Preflight failures remain outside this lifecycle: they do not create a result
summary and do not construct a provider. Once `RUNNING` is persisted, every
controlled outcome publishes exactly one immutable result summary before the
execution error is propagated.

## Terminal classification

| Runtime condition | Classification | Ledger |
|---|---|---|
| `SUBMIT`/`STOP`, evidence and all integrity checks pass | `LIVE_SMOKE_PASS` | `COMPLETE` |
| `MODEL_STEP_LIMIT`, `PROTOCOL_STALLED`, `WALL_TIME_LIMIT` | `LIVE_SMOKE_INTERACTION_NOT_READY` | `FAILED` |
| replay/persistence/accounting/invalid terminal/safety failure | `LIVE_SMOKE_HARNESS_FAILURE` | `FAILED` |
| provider construction/transport or `PROVIDER_ERROR` | `LIVE_SMOKE_PROVIDER_FAILURE` | `FAILED` |

The runtime terminal is retained in failure summaries. A separate bounded
`failure_artifact.json` is also written in the run directory when the attempt
has started; it records failure metadata without replacing the complete summary.

## Safety evidence

The summary now separates two kinds of claims:

- `runtime_measured_safety` is read from the runtime result fields
  `ground_truth_exposure`, `cross_scenario_evidence`, `writes`, and
  `arbitrary_execution`.
- `structural_safety_invariants` records architectural facts separately. It
  does not pretend that SQL, shell, arbitrary PromQL, fallback, or judge access
  are runtime counters when they are not instrumented as such.

Any nonzero measured runtime counter is classified as
`LIVE_SMOKE_HARNESS_FAILURE` before PASS can be emitted.

## Offline evidence

`tests/unit/itbench/test_live_smoke_execution.py` contains 22 passing tests for:

- the legal fake-provider PASS path and COMPLETE ledger;
- MODEL_STEP_LIMIT and PROTOCOL_STALLED summaries, FAILED ledgers, and rerun
  refusal;
- typed provider failure;
- replay mismatch and accounting mismatch persistence before propagation;
- nonzero runtime safety fail-closed behavior; and
- absence of judge/ground-truth-loader dependencies in the execution module.

The fake-provider test uses the existing synthetic Scenario-999 backend and
proves native artifact reload plus event replay. It is an offline structural
test, not a live result and not an RCA score.

## Immutability and rerun behavior

The executor refuses to start when `result_path` exists or when the run
directory exists. It does not reset a failed ledger. The terminal summary is
written atomically, and the first published result is not overwritten by later
CLI error handling.

## Validation status

At the time of this report:

| Check | Result |
|---|---|
| Targeted execution tests | PASS — 22 passed |
| Full pytest | PASS — 488 passed, 1 existing deprecation warning |
| Ruff check | PASS |
| Ruff format check | PASS |
| Mypy | PASS |
| Semantic-budget invariant | PASS |
| Simulated smoke v2 | PASS |

The remaining repository checks and CI are part of the final handoff gate. No
SMOKE-002 preregistration or real ledger was created.

## Network accounting

```text
live agent calls: 0
OpenAI outbound attempts: 0
Luna calls: 0
judge calls: 0
official benchmark scenarios: 0
live smoke scenarios: 0
```

## Historical boundary

This patch does not change E7, E8, E9, or SMOKE-001 evidence. It does not
claim RCA quality improvement. A future human-approved SMOKE-002 remains a
separate action.
