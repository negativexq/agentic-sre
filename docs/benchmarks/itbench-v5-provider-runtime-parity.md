# ITBench V5 provider/runtime parity

## Historical observation

SMOKE-004 remains immutable and remains `LIVE_SMOKE_PASS` with eight calls,
five semantic actions, one rejection, one recovered rejection, and `SUBMIT`.
The turn-6 `string_too_long` rejection was caused by
`RATIONALE_PROVIDER_LOCAL_LENGTH_DRIFT`: the local V5 contract capped rationale
at 300 characters while the provider branch had no corresponding bound.

## Repair

Named bounds now live in `packages/itbench_v5_contract.py` and are consumed by
both `ITBenchInvestigationDecisionV5` and the V5 provider schema builder:

| Field | Local contract | Old provider schema | New provider schema | Result |
| --- | --- | --- | --- | --- |
| `action` | `E9Action` | action-specific enum branch | action-specific enum branch | exact |
| `target` | C###, max 16 characters | enum, no explicit maxLength | current enum, maxLength 16 | exact exposed parity |
| `targets` | max 3; SUBMIT requires at least 1 | no cardinality bounds | minItems 1, maxItems 3 | exact cardinality |
| `operation` | max 64 characters | enum, no explicit maxLength | current enum, maxLength 64 | exact exposed parity |
| `rationale` | nullable, max 300 | nullable string, no maxLength | nullable string maxLength 300 | exact |
| `stop_reason` | STOP requires non-empty, max 128 | string, no maxLength | maxLength 128 | max exact; non-empty remains runtime-owned |

The generic V5 fallback also carries the defensive target-array maximum.

## Strict-root compatibility

The repaired request function remains action-specific but now has an object
root:

```text
request_itbench_tools.parameters.type = object
root anyOf = absent
properties.decision.anyOf = present
additionalProperties = false
all root properties required
```

Submit and stop remain separate object-root functions.

## Offline regression results

```text
rationale length 300 = valid
rationale length 301 = rejected by provider schema validator
SUBMIT 0 targets = rejected
SUBMIT 1–3 targets = valid
SUBMIT 4 targets = rejected
STOP reason length 128 = valid
STOP reason length 129 = rejected
initial/VERIFY action-specific gating = preserved
nested decision normalization = preserved
SMOKE-003 strict-root regression = preserved
```

No live provider, benchmark, or evaluator was used for this repair.

```text
live agent calls: 0
OpenAI outbound attempts: 0
Luna calls: 0
judge calls: 0
live smoke scenarios: 0
official benchmark scenarios: 0
```
