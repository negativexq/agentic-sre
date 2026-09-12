# ADR-003: Credit-aware single-agent baseline

- Status: Accepted
- Date: 2026-09-12

## Context

The first agent milestone must prove the investigation loop without turning
every development or CI run into a paid model call. The deterministic
telemetry, tool, evidence, and policy boundaries already established by
`v0.1.1` must remain authoritative.

## Decision

`v0.2.0` uses one explicitly configured OpenAI model boundary:

```text
model             = gpt-5.6-luna
reasoning_effort  = none
fallbacks         = none
```

The default provider is `FakeModelProvider`. Real model execution is disabled
unless `SRE_LIVE_MODEL_ENABLED=true` is explicitly set, and the API key is read
only from `OPENAI_API_KEY` in the process environment.

The runtime enforces:

- at most three model calls per incident;
- at most eight tool calls per incident;
- a read-only batch may consume only the remaining incident tool budget;
- no write tools, shell execution, remediation, or Kubernetes write access;
- a shared file-backed live-call budget configured by the operator;
- zero schema-repair calls and no fallback model.

The provider exposes three semantic decision functions. On the final allowed
model turn, only the terminal hypothesis and stop functions are exposed; a
tool-request function is not a valid future state on that turn.

Normal checks, CI, fake-provider integration, and offline benchmark grading do
not call OpenAI. Live provider and benchmark commands are explicit opt-in
operations.

## Consequences

- The investigation protocol is testable and reproducible without API credit.
- Batch tool planning keeps the expected incident path near two model calls.
- Live usage is visible through run-level call and token accounting.
- Model output never owns evidence IDs; only the runtime can create evidence.
- Provider integration remains isolated behind a typed boundary for later
  changes.
