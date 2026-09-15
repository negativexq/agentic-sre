# ITBench control-plane offline repair round two

This is an offline engineering record. Historical E7/E8/E9 result and smoke
artifacts are immutable. No E9 rerun, E10 benchmark, live smoke, model call,
OpenAI request, Luna request, or judge request occurred.

## Source verification

| Finding | Status | Evidence and repair |
|---|---|---|
| Distinct R1–R7 ablations | CONFIRMED / FIXED | The old script repeated one configuration. The v3 script uses a typed `E9ControlPlaneVariant`, per-variant flags and SHA256 configuration identity. |
| Canary counters | CONFIRMED / FIXED | v2 derives exposed/runtime surfaces from actual traces and checks the semantic executor surface. |
| Candidate-state trap | CONFIRMED / FIXED | Candidate history remains auditable, while projection exposes one current hypothesis and alternatives without a three-ACTIVE overflow trap. |
| Dynamic discovery visibility | CONFIRMED / FIXED | Newly discovered entities carry `discovered_turn`, are prioritized, and are included in provider-valid target handles. |
| Temporal sign rule | CONFIRMED / FIXED | `BEFORE`, `NEAR_ONSET`, `DURING`, `AFTER`, and `UNKNOWN` are distinct; support is conservative. |
| Semantic operation honesty | PARTIAL | Specification nesting and meaningless unknown trace edges were corrected. Snapshot limitations for true historical change and replica comparison are reported explicitly. |
| Structured memory packing | CONFIRMED / FIXED | `e9_memory` and `e9_semantic` use the same structured packer. Oversized dictionaries/lists remain structured with counts and truncation metadata. |
| Context selectivity | PARTIAL | Current/new candidate ordering, rejection visibility, and bounded alert sections are real. Full hypothesis-question ranking remains a future improvement. |
| Provider schema complexity | PARTIAL | Dynamic V5 surface is retained; schema complexity is now observable. No unnecessary action-specific schema rewrite was made. |
| Identity preflight | CONFIRMED / FIXED | Future live runner code validates identity, dataset, readiness, model policy and fresh budget before provider construction. It was not executed. |

## Offline evidence

The executable ablation harness ran eight configurations over thirteen scripted
trajectories. R0 disables recovery and produced 7 stalls out of 12. R1–R7
enable recovery and completed all 12. The v3 artifact includes feature flags,
configuration hashes, surface sizes, rejection/recovery counts, replay and
trace measurements; metrics are computed from FakeModelProvider executions.

The repaired 35-snapshot canary used the real snapshot backend and fake
provider. It measured 35/35 terminal outcomes, zero stale capabilities, zero
missing executors, zero replay mismatches, zero state divergence and complete
turn traces. The previous canary's literal zero counters were not reused.

The six canary modes also ran offline: basic, rejection recovery, duplicate
suppression, revision, dynamic discovery, and the 11-operation semantic
matrix. Recovery and duplicate modes each recovered one rejection and reached
`SUBMIT`; dynamic discovery made `C002` visible and provider-targetable; the
semantic matrix returned structured results with zero executor errors.

The real operation coverage matrix attempted all 11 operations on all 35
snapshots. It recorded zero exceptions. `RECENT_CHANGE_ANALYSIS` and
`VERIFY_TEMPORAL_ALIGNMENT` were not counted as useful where the snapshot did
not provide the required historical/timestamp evidence. Replica comparison
was also reported unavailable when distinct peer identities could not be
established.

## Changed implementation surface

| Path | Purpose | Test/evidence |
|---|---|---|
| `packages/evals/itbench/e9_control.py` | typed variant identity and current/new target surface | ablation v3, harness tests |
| `packages/evals/itbench/e9_memory.py` | reversible candidate projection, turn-aware discovery, shared structured packing | replay and five-revision tests |
| `packages/evals/itbench/e9_context.py` | independently packed alert digest and candidate-aware ordering | alert/context tests and canary v2 |
| `packages/evals/itbench/e9_runtime.py` | variant surfaces, semantic safe rejection, actual trace capability fields | full pytest and canary v2 |
| `packages/evals/itbench/e9_semantic.py` | shared packer, conservative temporal relation, honest spec/trace/replica results | semantic matrix and coverage artifact |
| `packages/evals/itbench/e9_packing.py` | single structured bounded packer | late-field/no-prefix test |
| `packages/evals/itbench/e9_fsm.py` | removed independent mutable phase source | FSM tests |
| `scripts/itbench_e9_internal_ablation.py` | executable R0–R7 feature configurations | `itbench-control-plane-ablation-v3.json` |
| `scripts/itbench_e9_control_plane_canary.py` | measured counters and adversarial offline modes | `itbench-control-plane-canary-v2.json` |
| `scripts/itbench_semantic_operation_coverage.py` | 35-snapshot operation coverage | semantic coverage JSON/MD |
| `scripts/itbench_control_plane_live_smoke.py` | future fail-closed preflight code only | not executed |
| `tests/unit/itbench/test_e9_harness.py` | regressions for state, packing, temporal logic, discovery, alert packing | 20 targeted tests |

The existing E9 `v2` ablation artifact was restored from HEAD after the
offline script was redirected; it remains historical and unmodified. New
results are only in the `v3` artifact.

## Operation coverage summary

See [`itbench-semantic-operation-coverage.md`](itbench-semantic-operation-coverage.md).
All 11 operations returned structured bounded values in 35/35 attempts with
zero errors. Useful-result counts were: incident overview 35, alert analysis
35, topology analysis 35, entity context 35, events 35, metrics 35, spec 35;
recent changes 0, trace error tree 0, replica comparison 0, and temporal
alignment 0 because the required evidence was unavailable or inconclusive.

Structured packing and temporal regression tests pass. Semantic-operation
coverage is generated by the offline matrix script; if a snapshot cannot
support a claimed semantic operation, the output is marked unavailable or
inconclusive rather than treated as useful evidence.

## Architecture boundary

`E9CaseMemory` is the source of operational state; `E9ControlSurface` derives
the exposed surface; `E9ContextPlanner` consumes that surface; and runtime
validation consumes the same surface. Candidate handles are scenario-local
and evidence provenance remains runtime-owned. No vector database, multi-agent
system, model upgrade, reasoning upgrade, or safety-boundary relaxation was
introduced.

## Historical integrity and network accounting

Historical E9 remains `ITB_E9_COMPLETE_REGRESSED`. This round produced no new
official predictions and did not run a live smoke. Required accounting is:

```text
live agent calls = 0
OpenAI outbound attempts = 0
Luna calls = 0
judge calls = 0
official benchmark scenarios = 0
live smoke scenarios = 0
```

The result is an offline candidate for human review only:
`READY_FOR_LIVE_SMOKE_REVIEW`.
