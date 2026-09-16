# Control live-smoke execution readiness

This is an offline readiness artifact only. No SMOKE-002 preregistration or
real ledger was created, and no provider was contacted.

## Execution architecture

```text
typed manifest
  → provider-free preflight
  → fresh dedicated budget
  → manifest-bound limits/policy
  → provider factory
  → Scenario-999 observable runtime
  → atomic artifacts
  → strict reload + event replay
```

The implementation is split across:

- `packages/evals/itbench/live_smoke_contract.py`: canonical typed manifest and
  shared relevant-path definition.
- `packages/evals/itbench/live_smoke_preflight.py`: fail-closed validation;
  it does not import or construct a provider.
- `packages/evals/itbench/live_smoke_execution.py`: dedicated executor whose
  runtime limits and provider retry policy come from the validated manifest.
- `scripts/itbench_control_plane_live_smoke_execute.py`: thin future CLI.

## Offline proof

The fake end-to-end path used three provider interactions:

```text
HYPOTHESIZE C001
→ INVESTIGATE C001 / ENTITY_CONTEXT
→ STOP
```

It created semantic evidence, persisted native/runtime artifacts, reloaded
them, and verified event replay equality. The terminal was `STOP`. The fake
provider had three invocations, but zero OpenAI outbound attempts; its budget
therefore remained `0 used / 8 remaining`.

Preflight failure tests verified that the provider factory is not called for
dataset drift. The provider retry binding test verified `max_retry=0` using a
network-free stub. Manifest values produce:

```text
E9Limits(8 model calls, 8 semantic/tool calls, 8 turns, 180 seconds)
max_consecutive_rejected_actions = 2
```

## Fixture and safety

The future fixture contract is synthetic `Scenario-999`, non-official, with no
ground truth loaded. The execution module has no judge/evaluator path. The
result schema explicitly records zero fallback, ground-truth access,
cross-scenario access, remediation, shell, SQL, arbitrary PromQL, and judge
access.

## Future sequencing

The actual SMOKE-002 preregistration must be generated only after the final
source SHA is reviewed and frozen. It must not be committed before the later
separately approved execution, because preflight compares the exact Git HEAD.
This task does not create that preregistration or ledger.

## Network accounting

```text
live agent calls: 0
OpenAI outbound attempts: 0
Luna calls: 0
judge calls: 0
official benchmark scenarios: 0
live smoke scenarios: 0
```

## Readiness

Offline execution-harness checks passed. Real provider schema and transport
behavior remain a later human-approved risk.
