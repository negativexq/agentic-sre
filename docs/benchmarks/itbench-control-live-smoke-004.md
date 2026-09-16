# ITB-CONTROL-LIVE-SMOKE-004

## Identity and policy

This was the single authorized live interaction smoke on the synthetic
`Scenario-999` fixture. The runtime source identity was:

```text
54ed46677dfc3f33127882b5138f394676b7569a
```

The manifest and runtime recorded:

```text
provider = openai
model = gpt-5.6-luna
reasoning_effort = none
provider_retries = 0
max provider calls = 8
max model turns = 8
max semantic actions = 8
max wall time = 180 seconds
judge disabled = true
fixture = Scenario-999 (synthetic)
ground_truth_loaded = false
```

Provider-free preflight passed. The provider was not constructed before
preflight. The relevant worktree was clean and the strict-root assertion
passed: `request_itbench_tools.parameters.type` was `object`, with no
root-level `anyOf` and a nested `properties.decision.anyOf`.

## Classification and terminal

```text
classification = LIVE_SMOKE_PASS
terminal = SUBMIT
native terminal = SUBMIT_DIAGNOSIS
```

OpenAI accepted the repaired `request_itbench_tools` schema. Eight provider
calls produced parsed responses; no provider failure occurred.

## Per-turn trajectory

| Turn | Phase | Context chars | Exposed actions | Exposed operations | Targets | Model action | Result |
| ---: | --- | ---: | --- | --- | --- | --- | --- |
| 1 | OBSERVE | 2147 | HYPOTHESIZE, STOP | — | C001, C002 | HYPOTHESIZE C001 | accepted; VERIFY |
| 2 | VERIFY | 3080 | INVESTIGATE, REVISE, STOP | ENTITY_CONTEXT, EVENT_ANALYSIS, METRIC_ANOMALIES, TRACE_ERROR_TREE, SPEC_ANALYSIS | C001, C002 | INVESTIGATE C001 / EVENT_ANALYSIS | accepted; E001 |
| 3 | VERIFY | 3425 | INVESTIGATE, REVISE, SUBMIT, STOP | ENTITY_CONTEXT, METRIC_ANOMALIES, TRACE_ERROR_TREE, SPEC_ANALYSIS | C001, C002 | INVESTIGATE C001 / METRIC_ANOMALIES | accepted; E002 |
| 4 | VERIFY | 3883 | INVESTIGATE, REVISE, SUBMIT, STOP | ENTITY_CONTEXT, TRACE_ERROR_TREE, SPEC_ANALYSIS | C001, C002 | INVESTIGATE C001 / ENTITY_CONTEXT | accepted; E003 |
| 5 | VERIFY | 4351 | INVESTIGATE, REVISE, SUBMIT, STOP | TRACE_ERROR_TREE, SPEC_ANALYSIS | C001, C002 | INVESTIGATE C001 / TRACE_ERROR_TREE | accepted; E004 |
| 6 | VERIFY | 4666 | INVESTIGATE, REVISE, SUBMIT, STOP | SPEC_ANALYSIS | C001, C002 | invalid safe action shape | rejected; `string_too_long` |
| 7 | VERIFY | 4907 | INVESTIGATE, REVISE, SUBMIT, STOP | SPEC_ANALYSIS | C001, C002 | INVESTIGATE C001 / SPEC_ANALYSIS | accepted; E005 |
| 8 | VERIFY | 5042 | SUBMIT, STOP | — | C001, C002 | SUBMIT C001 | accepted; CONCLUDE |

The turn-6 rejection was recovered on turn 7. The persisted rejection reason
was `safe action shape was invalid`; no repeated impossible capability was
introduced.

## Semantic operations and evidence

Chronological semantic actions were:

```text
EVENT_ANALYSIS(C001)    → E001
METRIC_ANOMALIES(C001)  → E002
ENTITY_CONTEXT(C001)    → E003
TRACE_ERROR_TREE(C001)  → E004
SPEC_ANALYSIS(C001)     → E005
```

```text
semantic actions requested = 5
semantic actions executed = 5
evidence count = 5
```

The submitted candidate was C001 and had five associated evidence items.
No dynamic candidate was discovered during this run.

## Accounting and context

```text
model calls = 8
provider invocations = 8
outbound API attempts = 8
successful model responses = 8
input tokens = 11129
output tokens = 522
provider latency = 20292 ms
wall time = 20971 ms
action rejections = 1
recovered action rejections = 1
ledger cap = 8
ledger calls_used = 8
ledger remaining = 0
ledger status = COMPLETE
```

Context sizes by turn were:

```text
2147, 3080, 3425, 3883, 4351, 4666, 4907, 5042
```

Aggregate context size:

```text
minimum = 2147
median = 4117
maximum = 5042
```

## Persistence and replay

Persisted run artifacts were:

```text
native_artifact.json
turn_trace.json
case_state.json
event_log.json
usage.json
agent_output.json
```

Strict reload passed for the native artifact, agent output, turn trace, case
state, and event log. Event replay projection equaled the persisted case
state. Trace completeness was exact:

```text
turn trace entries = 8
model steps = 8
```

Provider accounting and the dedicated ledger reconciled exactly at eight
calls.

## Safety

Runtime-measured safety counters were all zero:

```text
ground_truth_exposure = 0
cross_scenario_evidence = 0
writes = 0
arbitrary_execution = 0
```

Structural safety invariants held: no judge path, ground-truth loader,
provider fallback, shell, SQL, arbitrary PromQL, remediation, or
cross-scenario loader was present in the execution path.

## Interpretation

This one smoke demonstrates that the repaired strict-root V5 function schema
was accepted by the real provider, nested decisions were usable by the
runtime, Luna could select candidate-specific semantic operations, a safe
local rejection was recovered, and the bounded execution reached a valid
terminal with exact persistence/replay and accounting.

It does not establish RCA quality, E10 performance, official ITBench score,
general Luna reliability, or readiness for an official benchmark. No judge
was run and no ground truth was loaded.

Historical chronology remains unchanged: SMOKE-001 failed before network,
SMOKE-002 had one provider-boundary failure without precise diagnostics,
SMOKE-003 exposed the root-`anyOf` defect, and SMOKE-004 is this single
diagnostic live execution.

```text
reruns performed = 0
official benchmark scenarios = 0
judge calls = 0
E10 started = false
```
