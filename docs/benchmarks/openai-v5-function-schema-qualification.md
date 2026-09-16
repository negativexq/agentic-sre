# OpenAI V5 function-schema qualification

Offline-only qualification for the provider wire-schema repair motivated by SMOKE-003. No OpenAI request was made. The generated JSON contains the exact parameter schemas for the representative states.

Source used for generation:

```text
1614172a4f39f791a143ecfc97db67ad712635d5
```

## Root contract

Every exposed function passed the local strict validator:

```text
root type = object
root anyOf = absent
root properties = object
all root properties required = true
additionalProperties = false
```

Nested unions remain allowed. For `request_itbench_tools`, the action union is at:

```text
properties.decision.anyOf
```

This directly fixes the SMOKE-003 failure path: `tools[0].parameters.type` is `object`, and `tools[0].parameters` has no root-level `anyOf`.

V5 runtime/provider bounds are also present in the generated schemas:

```text
rationale.maxLength = 300
submit.targets.minItems = 1
submit.targets.maxItems = 3
stop_reason.maxLength = 128
```

## Representative surfaces

| State | Function | Root | Root anyOf | Nested union | Branches | Action enums | Target counts | Operation counts | Result |
| --- | --- | --- | --- | --- | ---: | --- | --- | --- | --- |
| INITIAL | `request_itbench_tools` | object | false | `properties.decision.anyOf` | 1 | HYPOTHESIZE | 2 | 0 | PASS |
| INITIAL | `stop_itbench_investigation` | object | false | none | 0 | — | — | — | PASS |
| VERIFY | `request_itbench_tools` | object | false | `properties.decision.anyOf` | 2 | INVESTIGATE, REVISE | 1, 2 | 3, 0 | PASS |
| VERIFY | `stop_itbench_investigation` | object | false | none | 0 | — | — | — | PASS |
| VERIFY_WITH_SUBMIT | `request_itbench_tools` | object | false | `properties.decision.anyOf` | 2 | INVESTIGATE, REVISE | 1, 2 | 3, 0 | PASS |
| VERIFY_WITH_SUBMIT | `submit_itbench_diagnosis` | object | false | none | 0 | — | — | — | PASS |
| VERIFY_WITH_SUBMIT | `stop_itbench_investigation` | object | false | none | 0 | — | — | — | PASS |
| FINAL | `submit_itbench_diagnosis` | object | false | none | 0 | — | — | — | PASS |
| FINAL | `stop_itbench_investigation` | object | false | none | 0 | — | — | — | PASS |

## Action-specific gating

The nested V5 branches preserve the existing legal combinations:

```text
INITIAL
  HYPOTHESIZE → C001/C002
  STOP

VERIFY
  INVESTIGATE → target C001, operations ENTITY_CONTEXT/EVENT_ANALYSIS/SPEC_ANALYSIS
  REVISE → targets C002/C003
  STOP

VERIFY_WITH_SUBMIT
  same investigation/revision branches
  SUBMIT → evidence-qualified C001
  STOP
```

The schema does not represent the old flat cartesian product. `INVESTIGATE` cannot target an alternative, action-only branches cannot carry an operation, and `REVISE` cannot select the current hypothesis.

## Provider normalization

The provider accepts the wire shape:

```json
{
  "decision": {
    "action": "HYPOTHESIZE",
    "target": "C001",
    "rationale": null
  }
}
```

and returns the unchanged internal flat V5 decision shape:

```json
{
  "action": "HYPOTHESIZE",
  "target": "C001",
  "targets": [],
  "operation": null,
  "rationale": null,
  "stop_reason": null
}
```

INVESTIGATE normalization and existing SUBMIT/STOP normalization are covered by offline tests.

## Validation and accounting

Targeted provider and harness tests passed after the repair. No historical SMOKE-001, SMOKE-002, or SMOKE-003 artifact was modified. No SMOKE-004 preregistration or ledger was created.

```text
live agent calls: 0
OpenAI outbound attempts: 0
Luna calls: 0
judge calls: 0
live smoke scenarios: 0
official benchmark scenarios: 0
```
