# ITB-CONTROL-LIVE-SMOKE-003

This document is derived from the immutable SMOKE-003 JSON result and runtime artifacts. It is an interaction smoke, not an RCA score run.

## Preflight and identity

Preflight passed before provider construction:

```text
runtime source SHA: c249777ecbf533a1a9b10192c817846275722d32
runtime bundle SHA: f1f7b45788f58d2190131530b0c1f621131290d8811fe3fe654f50cdd9b23d00
relevant worktree dirty: false
provider constructed before preflight: false
fixture: Scenario-999 synthetic
ground truth loaded: false
judge disabled: true
```

## Result

```text
classification: LIVE_SMOKE_PROVIDER_FAILURE
terminal: PROVIDER_ERROR
failure stage: PROVIDER_TRANSPORT
execution: ITB-CONTROL-LIVE-SMOKE-003
```

The single provider attempt failed before a model response. The preserved diagnostic is:

```text
stable code: BAD_REQUEST
category: BAD_REQUEST
HTTP status: 400
exception class: BadRequestError
API error type: invalid_request_error
API error code: invalid_function_parameters
API error param: tools[0].parameters
request ID: req_fcd1a691c34346c5b0e743e560c642e8
```

The bounded provider message states that the `request_itbench_tools` function schema was rejected because its parameters were received as JSON Schema type `None` instead of an object. This is diagnostic evidence only; no source change or retry was made.

## Turn trajectory

There was one recorded provider turn:

| Turn | Phase | Context chars | Exposed actions | Exposed operations | Exposed targets | Model action | Accepted |
| ---: | --- | ---: | --- | --- | --- | --- | --- |
| 1 | `OBSERVE` | 2147 | `HYPOTHESIZE`, `STOP` | none | `C001`, `C002` | no structured model action; provider error | false |

The runtime and provider surfaces agreed on that turn. No semantic operation ran, no evidence was created, and no action rejection/recovery occurred.

## Accounting

```text
model calls: 1
provider invocations: 1
outbound API attempts: 1
successful model responses: 0
input tokens: 0
output tokens: 0
provider retries: 0
provider latency: 0 ms
semantic actions requested/executed: 0 / 0
action rejections/recovered: 0 / 0
evidence count: 0
budget: cap 8, used 1, remaining 7
```

The ledger is `FAILED`; the single-attempt rule prevents reuse or rerun.

## Persistence and safety

Persisted run artifacts include `native_artifact.json`, `turn_trace.json`, `case_state.json`, `event_log.json`, `usage.json`, `agent_output.json`, and `failure_artifact.json`. The final result contains the same provider-failure metadata as the failure artifact.

The runtime recorded:

```text
ground_truth_exposure: 0
cross_scenario_evidence: 0
writes: 0
arbitrary_execution: 0
```

Structural invariants recorded false for judge path, ground-truth loader, provider fallback, shell, SQL, arbitrary PromQL, remediation, and cross-scenario loader paths.

## Historical chain and interpretation

```text
SMOKE-001: preflight failure, 0 outbound attempts
SMOKE-002: PROVIDER_UNAVAILABLE, 1 outbound attempt, no diagnostic metadata
SMOKE-003: BAD_REQUEST / invalid_function_parameters, 1 outbound attempt
```

SMOKE-003 establishes that the new provider diagnostics preserve a conclusive status, API error fields, and request ID. It does not establish RCA quality, official ITBench quality, or that the schema issue is fixed.

```text
reruns performed: 0
official benchmark scenarios: 0
judge calls: 0
E10 started: false
```
