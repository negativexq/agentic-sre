# ADR-004: Bounded investigation protocol

- Status: Accepted
- Date: 2026-09-12

## Decision

The deterministic runtime is authoritative for decision availability, budgets,
tool registration, evidence provenance, and termination. The model receives
production alert scope, a bounded tool catalog, and compact cross-turn
progress. It does not receive backend query syntax or arbitrary execution
capabilities.

The normal model turn exposes three semantic decisions:

```text
request_investigation_tools
submit_root_cause_hypothesis
stop_investigation
```

The final allowed turn exposes only the two terminal decisions. A valid
terminal decision is processed before model-budget exhaustion is considered;
using the final call is not itself a `MODEL_CALL_LIMIT` termination.

Live backend targets must be explicit. Hidden service or consumer defaults are
not permitted because they can silently move an investigation to another
component. Tool results retain their effective observation window, and reused
providers record per-run accounting deltas rather than cumulative counters.

## Consequences

- A three-call investigation has a coherent terminal state.
- Tool planning remains batched and bounded by the total incident budget.
- Alert scope and capability descriptions reduce exploratory calls without
  embedding benchmark truth.
- Kubernetes and change inspection remain read-only and separately audited.
- An incomplete capability matrix blocks the paid benchmark before any live
  model call is made.
