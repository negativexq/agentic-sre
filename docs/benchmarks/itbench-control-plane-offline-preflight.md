# ITBench E9 control-plane offline preflight

Status: `READY_FOR_LIVE_SMOKE_REVIEW`. This document does not authorize or
perform a live smoke. Live model calls, Luna calls, judge calls, and official
benchmark scenarios are all zero.

## A–C. Historical findings and repairs

The immutable E9 baseline was 35/35 scenarios, 32 `PROTOCOL_STALLED`, 2
`MODEL_STEP_LIMIT`, 1 `STOP`, 0 `SUBMIT`, and Luna F1 `0.0`. The source audit
confirmed the rejection-feedback, phase/counter split, non-gated V5 surface,
missing/placeholder semantic executors, blind semantic packing, and stale
provenance defects. Exact A–V classifications are in
[`itbench-e9-engineering-postmortem.md`](itbench-e9-engineering-postmortem.md).

The repair keeps historical E9 runtime/result identity immutable. Current
policy is one event-derived CaseState projection, a shared deterministic
control surface, dynamic provider capabilities, bounded semantic evidence,
runtime-owned candidate identity/provenance, and replayable memory.

## Changed files

| Path | Purpose | Covered by |
|---|---|---|
| `packages/evals/itbench/e9_control.py` | Shared deterministic capability surface. | Harness tests, canary. |
| `packages/evals/itbench/e9_memory.py` | Rejection feedback and event-derived projection. | Replay/recovery tests, ablation. |
| `packages/evals/itbench/e9_fsm.py` | Stateless compatibility policy. | Harness tests. |
| `packages/evals/itbench/e9_context.py` | Phase-aware compact context. | 35-snapshot qualification, context tests. |
| `packages/evals/itbench/e9_runtime.py` | Runtime validation, recovery, and complete traces. | Harness tests, ablation, canary. |
| `packages/evals/itbench/e9_semantic.py` | Explicit bounded semantic executors and packing. | Registry/semantic tests. |
| `packages/evals/itbench/e9_identity.py` | Automatic Git/content identity and fail-closed validation. | Identity mismatch test. |
| `packages/provider/contracts.py` | Carries dynamic V5 capability surface. | Provider tests. |
| `packages/provider/openai.py` | Builds provider schema from dynamic capabilities. | Schema/parity tests. |
| `scripts/itbench_e9_internal_ablation.py` | Executes real offline variants. | 8×12 ablation artifact. |
| `scripts/itbench_e9_control_plane_canary.py` | 35-snapshot no-network canary. | Canary artifact. |
| `tests/unit/itbench/test_e9_harness.py` | Regression coverage for repaired control plane. | Full pytest. |

## D–G. Control architecture and semantic operations

`E9CaseMemory` is the event log plus materialized projection. `control_surface`
derives phase, actions, target handles, operations, and rejection budget from
that projection. `E9ContextPlanner` and the OpenAI function schema consume the
same surface; runtime validation consumes it again. The compatibility `E9FSM`
contains no independent mutable phase.

Every registered semantic operation has an executor and a bounded result. The
operation matrix is recorded in the JSON companion. Recent changes reports
observable object fields and explicitly says when historical before/after data
is unavailable. Spec, replica, trace, and temporal operations now use honest
structured semantics instead of generic raw-record labels.

## H. Memory/replay and rejection recovery

Safe rejection is represented by an event-derived `last_rejection` containing
the attempted action/target/operation, code, reason, and valid alternatives.
The next context exposes this correction; an accepted action clears the
feedback and resets the consecutive counter. Security, leakage, corruption,
and provider/infrastructure failures remain fail-closed.

Replay equality is asserted from the event log against the persisted
projection. Targeted tests and the executable ablation suite cover normal
trajectories, rejection/recovery, duplicate operations, revision, and terminal
transitions. The 35-snapshot canary had zero replay mismatches.

## I–K. Real offline ablations and canary

The replacement ablation harness is executable, not a tuple of declared
results: 8 variants × 12 distinct scripted trajectories, with a fake provider,
real runtime, and replay/trace assertions. R0 produced 7 stalls and 41.7%
completion. R1–R7 each produced 0 stalls and 100% completion, with 10
rejections, 8 recoveries, 0 replay mismatches, and complete traces.

The control-plane canary ran across all 35 frozen snapshots with the real
snapshot backend and no model/GT access. It produced 35 `SUBMIT` terminals,
0 harness-induced stalls, 0 state divergence, 0 stale capabilities, 0 missing
executors, 0 replay mismatches, and 100% trace completeness. This is a
structural control result, not an RCA score.

## L–N. Context and performance

Zero-network context qualification passed for all 35 snapshots. The immutable
historical E9 qualification was min `10,190`, median `11,676`, p95 `12,200`,
max `12,338` characters. The repaired canary’s first step was min `6,718`,
median `8,482`, p95 `8,844`, max `8,924` characters. The historical artifact
does not retain a separate Scenario-105 label; the repaired canary retains its
all-scenario distribution. Semantic packing is recursive and section-bounded
and never re-embeds an oversized original dictionary. Backend source-
performance counters remain attached per scenario in the canary artifact.

## O–P. Safety and remaining risks

All safety counters are zero: no GT exposure, cross-case evidence, writes,
remediation, shell, arbitrary filesystem access, SQL, arbitrary PromQL, or
fabricated evidence acceptance. No vector database, multi-agent architecture,
model routing, model upgrade, or reasoning change was introduced.

Remaining risks are semantic quality of model-facing evidence and the honesty
limits of snapshot data where historical diffs or peer replica identities are
unavailable. These are not masked by the harness.

## Q. Live-call readiness recommendation

`READY_FOR_LIVE_SMOKE_REVIEW` means all offline gates passed and a human may
review whether to authorize a future live smoke. The smoke itself was not run,
and no new benchmark identity or prediction artifacts were created.
