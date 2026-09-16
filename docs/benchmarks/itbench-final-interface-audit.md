# ITBench final interface audit (offline)

Reviewed source baseline: `15d837d9d1673cdb1085c4c63bfe6624ccbbb211`.
This audit used source and observable snapshots only. Historical E7/E8/E9
artifacts were not changed; live/model/judge calls were zero.

| Finding | Status | Evidence | Repair |
|---|---|---|---|
| A. Redundant initial OBSERVE operations | CONFIRMED | `E9ContextPlanner` already projects alerts, topology, candidates and incident metadata; `control_surface` exposed rediscovery operations | Initial surface is `HYPOTHESIZE`/`STOP`; overlap is measured offline |
| B. EVENT_ANALYSIS global scope | CONFIRMED | Registry omitted `EVENT_ANALYSIS` from `_TARGET_OPERATIONS`, although executor passed an entity filter | TARGET scope, VERIFY-only, candidate-specific event query |
| C. METRIC_ANOMALIES too permissive | CONFIRMED | Resolver defaulted unknown operations to available; prior coverage was provider-exposed 35/35 versus useful 1/35 | Availability requires meaningful candidate-filtered metric aggregate signal |
| D. V5 cross-field ambiguity | CONFIRMED | Generic schema constrained fields independently, while runtime had to reject combinations | Action-specific strict `anyOf` branches plus shared runtime capability surface |

The machine-readable record is [itbench-final-interface-audit.json](itbench-final-interface-audit.json).

## Scope decision

This is a narrow interface simplification. Existing event-derived state,
replay, structured packing, candidate discovery and safety boundaries remain
in place. No model, reasoning setting, budget, live runner, official run or
judge path was executed.
