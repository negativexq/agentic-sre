# ITBench E9 engineering postmortem

This is an offline repair record. Historical E9 artifacts remain immutable;
no E9 scenario was rerun and no E10 identity was started.

## Scope correction

The supplied “21 MODEL_DECISION_INVALID” count belongs to the durable E8
baseline. Durable E9 official artifacts contain 32 `PROTOCOL_STALLED`, 2
`MODEL_STEP_LIMIT`, 1 `STOP`, and 0 `SUBMIT`; the 21-invalid E8 evidence is not
relabelled as E9. E9’s dominant failure was repeated safe-action rejection
followed by stall.

## Source findings

| Finding | Classification | Verified behavior | Repair |
|---|---|---|---|
| A | CONFIRMED | Rejection detail was not projected into the next context. | Event-derived `last_rejection` with attempted action, code, reason, and valid alternatives. |
| B | CONFIRMED | Mutable FSM phase and memory phase could disagree. | CaseState-derived `control_surface`; FSM is stateless compatibility policy. |
| C | CONFIRMED | Rejection counters had multiple sources. | Reducer-owned counters and one stall decision. |
| D | CONFIRMED | V5 provider surface was not phase-gated. | Dynamic action/operation enums are built from the same surface as runtime validation. |
| E | CONFIRMED | `RECENT_CHANGE_ANALYSIS` lacked a real executor. | Explicit bounded executor and registry test. |
| F | PARTIALLY_CONFIRMED | Spec operation was a generic context wrapper. | Structured specification fields; absent historical diff is reported honestly. |
| G | CONFIRMED | Replica operation did not compare peers. | Honest availability result and bounded comparison surface. |
| H | CONFIRMED | Trace operation returned samples, not an error tree. | Grouped service/status edges. |
| I | CONFIRMED | Temporal operation did not calculate alignment. | Deterministic timestamp delta and tri-state result. |
| J | CONFIRMED | Initial candidate set behaved as a closed world. | Observable related entities can receive immutable handles. |
| K | PARTIALLY_CONFIRMED | Context selection was mostly fixed serialization. | Phase-aware bounded projection; richer question ranking remains a risk. |
| L | CONFIRMED | Runtime directly mutated state fields. | Events are appended; reducer materializes operational state. |
| M | CONFIRMED | Some reducer reset logic was unreachable. | Explicit accepted-action reset and replay tests. |
| N | CONFIRMED | Rejection/stall checks were scattered. | Central rejection payload/event path. |
| O | CONFIRMED | Accepted model steps were incompletely represented in traces. | One structured trace record per model step. |
| P | CONFIRMED | Historical A0–A6 file was declarative, not measured. | `internal-ablations-v2` executes real fake-provider variants. |
| Q | CONFIRMED | Historical Smoke-003 transport success was too weak as readiness evidence. | Offline readiness includes replay, trace, stale-capability, and recovery checks. |
| R | CONFIRMED | Semantic output used blind JSON-prefix truncation. | Recursive bounded semantic packing. |
| S | PARTIALLY_CONFIRMED | Candidate status paths could be rigid or bypass limits. | Central bounded status checks with explicit auditable reconsideration. |
| T | CONFIRMED | Submit preconditions were too weak. | Every submitted target needs candidate-associated evidence. |
| U | CONFIRMED | Context/evidence path repeated avoidable work. | Digests, relevant topology, and existing indexes. |
| V | CONFIRMED | Historical runtime SHA was recorded incorrectly. | Separate provenance errata; no historical artifact rewrite. |

Machine-readable detail is in
[`itbench-e9-engineering-postmortem.json`](itbench-e9-engineering-postmortem.json).

## Real offline evidence

The replacement ablation harness ran 8 variants over 12 distinct scripted
trajectories. R0, which disables rejection recovery, produced 7 stalls and
41.7% completion. R1–R7 each produced 0 stalls, 100% completion, 10 total
rejections, 8 recovered rejections, 0 replay mismatches, and complete traces.
These are control-plane metrics, not RCA scores and were not selected using
official ground truth.

The 35-snapshot control-plane canary used the real snapshot backend and
`FakeModelProvider`. It produced 35 valid `SUBMIT` terminals, 0 stalls, 0 state
divergences, 0 replay mismatches, 0 missing executors, 0 stale capabilities,
and 100% trace completeness. It made no model or judge calls.

## Safety and methodology

Ground truth was not loaded by runtime qualification. The repair introduced no
write/remediation, shell, SQL, arbitrary PromQL, or cross-scenario memory path.
The historical `ITB_E9_COMPLETE_REGRESSED` classification is unchanged.

This work ends at offline readiness review. A live smoke remains explicitly
unrun and requires human approval.
