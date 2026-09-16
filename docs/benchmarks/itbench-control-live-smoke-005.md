# ITB control live smoke 005

This is one bounded interaction smoke, not E10, an official ITBench run, or an RCA score run.

## Identity and preflight

- Execution: `ITB-CONTROL-LIVE-SMOKE-005`
- Frozen runtime source SHA: `2ec5afee60f15953f5e67d3108c8073984541432`
- Runtime bundle SHA: `3726b07478de017e3340eb1cbbe04dbba1abfb311e6623833099b4f7c95d7a53`
- Fixture: synthetic `Scenario-999`; official scenario `false`; ground truth loaded `false`
- Model policy: OpenAI `gpt-5.6-luna`, reasoning `none`, retries `0`
- Preflight: `PASS`
- Provider constructed before preflight: `false`
- Relevant worktree dirty: `false`
- Runtime limits: 8 model calls, 8 turns, 8 semantic actions, 180 seconds, 2 consecutive rejections

## Result

- Classification: `LIVE_SMOKE_PASS`
- Terminal: `SUBMIT`
- Provider constraint schema accepted: `true`
- Provider/local contract rejections: `0`
- Contract-drift rejections: `0`
- V5 contract live gate: `PASS`

The successful requests included the strict-root nested `decision.anyOf` V5 tool schema and the bounded `maxLength`, `minItems`, and `maxItems` declarations. This demonstrates provider acceptance of the declarations; it is not a separate boundary-value enforcement test.

## Provider diagnostics

No provider failure occurred. The typed diagnostic fields are therefore unavailable/null:

```json
{
  "stable_code": null,
  "category": null,
  "http_status_code": null,
  "exception_class": null,
  "api_error_type": null,
  "api_error_code": null,
  "api_error_param": null,
  "request_id": null,
  "message_summary": null
}
```

## Per-turn trajectory

| Turn | Phase | Exposed actions | Exposed operations | Exposed targets | Model decision | Target | Accepted | Evidence | Phase after |
|---:|---|---|---|---|---|---|---|---|---|
| 1 | OBSERVE | HYPOTHESIZE, STOP | none | C001, C002 | HYPOTHESIZE | C001 | yes | none | VERIFY |
| 2 | VERIFY | INVESTIGATE, REVISE, STOP | ENTITY_CONTEXT, EVENT_ANALYSIS, METRIC_ANOMALIES, TRACE_ERROR_TREE, SPEC_ANALYSIS | C001, C002 | INVESTIGATE | C001 | yes | E001 | VERIFY |
| 3 | VERIFY | INVESTIGATE, REVISE, SUBMIT, STOP | ENTITY_CONTEXT, METRIC_ANOMALIES, TRACE_ERROR_TREE, SPEC_ANALYSIS | C001, C002 | INVESTIGATE | C001 | yes | E002 | VERIFY |
| 4 | VERIFY | INVESTIGATE, REVISE, SUBMIT, STOP | ENTITY_CONTEXT, TRACE_ERROR_TREE, SPEC_ANALYSIS | C001, C002 | INVESTIGATE | C001 | yes | E003 | VERIFY |
| 5 | VERIFY | INVESTIGATE, REVISE, SUBMIT, STOP | ENTITY_CONTEXT, TRACE_ERROR_TREE | C001, C002 | INVESTIGATE | C001 | yes | E004 | VERIFY |
| 6 | VERIFY | INVESTIGATE, REVISE, SUBMIT, STOP | ENTITY_CONTEXT | C001, C002 | INVESTIGATE | C001 | yes | E005 | VERIFY |
| 7 | VERIFY | REVISE, SUBMIT, STOP | none | C001, C002 | REVISE | C002 | yes | none | VERIFY |
| 8 | VERIFY | SUBMIT, STOP | none | C001, C002 | SUBMIT | C001 | yes | none | CONCLUDE |

Semantic operations, in order: `EVENT_ANALYSIS`, `METRIC_ANOMALIES`, `SPEC_ANALYSIS`, `TRACE_ERROR_TREE`, `ENTITY_CONTEXT`.

There were no action rejections and no recovery events. The final submission contained `C001`; this smoke does not evaluate whether that candidate is the privileged or correct RCA.

## Accounting and context

- Model calls / provider invocations / outbound attempts: `8 / 8 / 8`
- Successful model responses: `8`
- Input tokens: `11,713`
- Output tokens: `492`
- Provider latency: `18,531 ms`
- Wall time: `19,197 ms`
- Semantic actions requested / executed: `5 / 5`
- Evidence items: `5`
- Action rejections / recovered rejections: `0 / 0`
- Ledger: cap `8`, calls used `8`, remaining `0`, status `COMPLETE`

Context characters by turn: `2,147`, `3,149`, `3,494`, `3,952`, `4,424`, `4,740`, `5,158`, `5,991`.

Context aggregate: minimum `2,147`, median `4,188`, maximum `5,991` characters.

## Persistence, replay, and safety

Reloaded successfully: `native_artifact.json`, `turn_trace.json`, `case_state.json`, `event_log.json`, `usage.json`, and `agent_output.json`.

- Native artifact reload: `PASS`
- Event replay projection equals persisted case state: `PASS`
- Trace entries / model steps: `8 / 8`
- Provider accounting equals ledger accounting: `PASS`

Runtime-measured safety counters were all zero:

```json
{
  "ground_truth_exposure": 0,
  "cross_scenario_evidence": 0,
  "writes": 0,
  "arbitrary_execution": 0
}
```

Structural safety invariants were all satisfied: no judge path, ground-truth loader, cross-scenario loader, provider fallback, remediation path, shell execution path, SQL path, or arbitrary PromQL path was present.

## Historical chain and interpretation

- SMOKE-001: preflight failure, 0 outbound attempts.
- SMOKE-002: provider failure, 1 outbound attempt, cause not captured precisely.
- SMOKE-003: HTTP 400 `invalid_function_parameters`, root `anyOf` schema defect identified.
- SMOKE-004: first live pass, 8 successful responses, and one recovered local rationale-length rejection.
- SMOKE-005: final V5 constraint qualification, 8 successful responses, no contract-drift rejection, terminal `SUBMIT`.

This smoke demonstrates that the real provider accepted the repaired strict-root V5 schema and that the bounded Luna/runtime interaction remained coherent through semantic evidence, revision, submission, persistence, replay, and accounting. It does not establish RCA quality, official ITBench accuracy, general Luna sufficiency, or E10 readiness.

Hard stop: reruns performed = `0`; official benchmark scenarios = `0`; judge calls = `0`; E10 started = `false`.
