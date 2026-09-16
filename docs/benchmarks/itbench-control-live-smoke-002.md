# ITB control live smoke 002

## Identity and preflight

This was the one authorized live interaction against the synthetic
`Scenario-999` fixture. The runtime source was frozen before execution and the
provider-free preflight passed.

| Field | Value |
|---|---|
| Execution | `ITB-CONTROL-LIVE-SMOKE-002` |
| Frozen runtime source SHA | `314fa6576288fe60d395421e927ee4b46c7fd401` |
| Runtime bundle SHA | `5c720ccbf8f033e4b7cace29050b9251d1de44d682fd81e356799dabdbd4396c` |
| Fixture | synthetic `Scenario-999` |
| Ground truth loaded | `false` |
| Preflight | PASS |
| Provider constructed before preflight | No |
| Provider policy | OpenAI / `gpt-5.6-luna` / reasoning `none` / retries `0` |
| Runtime limits | 8 model calls, 8 semantic actions, 8 turns, 180 seconds |
| Rejection limit | 2 consecutive rejections |
| Judge | disabled |

## Classification

```text
LIVE_SMOKE_PROVIDER_FAILURE
```

The single provider request reached the provider boundary, but the provider
returned `PROVIDER_UNAVAILABLE`. No retry was attempted. This is not an E10
run, official benchmark, or judge run.

## Per-turn trajectory

| Turn | Phase before | Context chars | Exposed actions | Exposed operations | Exposed targets | Parsed action | Accepted | Evidence |
|---:|---|---:|---|---|---|---|---|---|
| 1 | `OBSERVE` | 2147 | `HYPOTHESIZE`, `STOP` | none | `C001`, `C002` | provider error: `PROVIDER_UNAVAILABLE` | no | none |

The first-turn surface contained no callable `INCIDENT_OVERVIEW`,
`ALERT_ANALYSIS`, or `TOPOLOGY_ANALYSIS` operation. The request therefore
exercised the intended compact initial interface, but Luna did not return a
structured action.

## Calls, tokens, and semantic work

| Metric | Value |
|---|---:|
| Model calls | 1 |
| Provider invocations | 1 |
| Outbound API attempts | 1 |
| Input tokens | 0 |
| Output tokens | 0 |
| Provider latency | 0 ms recorded in native runtime usage |
| Semantic actions requested | 0 |
| Semantic actions executed | 0 |
| Action rejections | 0 |
| Recovered rejections | 0 |
| Evidence items | 0 |
| Context chars | min/median/max = 2147 / 2147 / 2147 |

The provider accounting snapshot reports one invocation, one outbound attempt,
and zero provider retries. No semantic operation was selected because the
provider failed before returning a model action.

## Budget and persistence

```text
cap       = 8
calls_used = 1
remaining  = 7
status     = FAILED
```

The native runtime artifact, turn trace, case state, event log, usage,
agent-output normalization, failure artifact, and final result summary were
persisted. The result records the manifest SHA256 as
`e9cb2f4207749af22cd4ce3a70b28bd6b6ac7aec65803d43b112ec307ce194ed`.

Strict reload and event replay completed for the persisted native artifact.
The case remained in `OBSERVE`, with no hypothesis and no evidence. The result
artifact is immutable; the executor refuses another attempt when it exists.

## Safety

Runtime-measured safety from the native result:

```text
ground_truth_exposure = 0
cross_scenario_evidence = 0
writes = 0
arbitrary_execution = 0
```

Structural safety invariants recorded by the executor all hold: no judge path,
ground-truth loader, provider fallback, shell, SQL, arbitrary PromQL,
remediation, or cross-scenario loader is present in this execution path.

## What this smoke proves

- The frozen manifest and source identity passed provider-free preflight.
- The dedicated runner bound the declared 8-call/8-action/8-turn/180-second
  limits and zero-retry policy.
- The provider was not constructed before preflight.
- The compact initial action surface was delivered to the real provider.
- One provider failure was accounted once, without retry.
- Failure evidence, ledger state, native artifacts, replay, and trace were
  persisted.

## What this smoke does not prove

It does not measure RCA quality, F1, hypothesis quality, semantic tool choice,
rejection recovery, submission behavior, or benchmark improvement. The provider
was unavailable before Luna returned an action, so those behaviors remain
unmeasured.

## Hard stop

```text
reruns performed = 0
official benchmark scenarios = 0
judge calls = 0
E10 started = false
```
