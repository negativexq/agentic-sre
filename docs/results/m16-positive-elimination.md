# M16 Positive Elimination — Prerequisite Result

## Prerequisite — Missing proof versus contradiction

Date: 2026-09-24  
Frozen contract: `docs/architecture/m16-positive-elimination-contract.md`, `m16.v1` (unchanged)  
Contract baseline implementation HEAD: `b2cb0b22f6e02909bef17ea51e9d419214f3ca8c`  
Historical pre-repair control: `c4da0cc63a6be9d776cf8c84b54126db4c018548`  
Epistemic repair: `7e41b11cb9014296cc06c3bb046d70532bc8872d`  
Generic discovery-phase repair: `279fb964b3113524a5e027fd13a80b31e649959c`

This is an M16 prerequisite, not M16 Task 2. No positive-elimination rules were added. The sealed M15 artifact at `.local/eval/m15/transition-certified-v1` was not modified.

### Baseline defect and repair

At `c4da0cc`, `resolution.assess_hypothesis()` mapped `UNLINKED` to `NO_CAUSAL_SYMPTOM_LINK`, then treated that insufficiency reason as `CONTRADICTED`. With no Findings or positive contradictory evidence, `resolve_hypotheses()` consequently placed the hypothesis in `eliminated_hypotheses` and emitted a `ResolutionElimination` with code `NO_CAUSAL_SYMPTOM_LINK`.

At `7e41b11`, missing causal linkage and missing onset-capable initiating evidence leave a hypothesis `UNRESOLVED`. Only existing positive hard contradiction semantics can produce `CONTRADICTED`; temporal contradiction eliminations retain the exact hard-contradiction Finding IDs. The existing root-cause eligibility exclusion and evidence-backed supported-hypothesis dominance behavior remain separate and unchanged.

An official replay exposed a second generic defect in investigation phase selection. `_has_source_hypothesis()` treated a source-capable initiating Finding as sufficient to close `SOURCE_DISCOVERY`, even when the hypothesis had `causal_explanation=UNLINKED`. That prevented the restored unresolved hypotheses from receiving the discovery sequence needed to establish causal links. At `279fb96`, a source hypothesis only closes discovery when the source-capable initiating evidence is linked by `PATH` or `DIRECT`. This does not change hypothesis state or contradiction authority.

### Official replay and first-divergence attribution

The compatibility oracle was `packages/evals/itbench/investigation_benchmark.py::predict_investigations`, using `SnapshotSource`, `DeterministicIntentPolicy`, the frozen 25 TEST scenario IDs, six turns, and eight tool calls. Predictions were sealed separately before grading. No provider/model calls occurred. Full ordered action identity was `(scenario_id, action order, gap_id, capability, target, query)`.

| Run | Evaluated HEAD | Actions | Exact identity vs sealed M15 | Differing slots | Affected scenarios |
|---|---|---:|---:|---:|---:|
| Historical control A | `c4da0cc` | 150 | 150/150 | 0 | 0 |
| Epistemic repair B | `7e41b11` | 150 | 31/150 | 119 | 20 |
| Repair plus generic phase correction C | `279fb96` | 150 | 148/150 | 2 | 1 |

Run B's 20 first divergences were all at turn 1. Each changed the selected action from the existing namespace `incident_events` / `EVENT_SEQUENCE` discovery action to a legal service/deployment `logs` / `DEPENDENCY_HEALTH` action. Those log candidates already existed in A's frontier; their presence was not a new gap-generation defect. The generic phase predicate had prematurely de-prioritized the still-needed discovery intent. The 99 later differing slots were cascades from those 20 first divergences. Classification: 20 `SECONDARY_INFORMATION_GAP_DEFECT`, 99 `CASCADE_FROM_EARLIER_DIVERGENCE`.

After the phase correction, 19 of those 20 scenario action sequences returned to the control. The one remaining first divergence is Scenario-6, turn 5: A reads `events` for the otel-collector Pod under its actor-local `FAILURE_ONSET` gap; C reads `events` for the fraud-detection Pod under its newly open actor-local `FAILURE_ONSET` question. Both targets have unresolved, evidence-backed hypotheses and both event reads are authorized. The gap's bounded authorized-query set contains the per-actor event queries for the grouped onset hypotheses. C's turn-6 `events` read for otel-collector is a cascade/continuation of that same unresolved onset frontier; A instead reads `resource_pressure` for otel-collector. Thus the corrected replay has one `EXPECTED_UNRESOLVED_FRONTIER_EXPANSION` first divergence and one `CASCADE_FROM_EARLIER_DIVERGENCE`, with no unexplained expansion. The remaining exact action mismatch is retained as historical control evidence; M15 identity is not used to restore the invalid elimination.

Across the 119 run-B mismatch slots, field-difference counts overlap: `gap_id` 119, `capability` 78, `target` 114, `query` 72. There were no scenario action-count or action-order differences. After the generic phase correction, only two slots differ: both have a gap-ID difference, one also changes target and one also changes capability/query. These are the two Scenario-6 reads described above.

> M15 action replay is a historical control. It is not the authority for preserving a pre-existing epistemic defect.

### Newly unresolved hypotheses and gap audit

Across the full repaired 25-scenario initial-diagnosis set, 71 hypotheses newly remain `UNRESOLVED` rather than being falsely contradicted: 71 `EVIDENCE_BACKED_UNRESOLVED_HYPOTHESIS`, 0 structural-only actor misclassifications, 0 other. All have source evidence references, `causal_explanation=UNLINKED`, no contradictory Findings, no causal paths, no linked symptoms, and no structural basis. Seventy have one Finding; one has three. Sixty-two have an initiating Finding; the other nine have the separate missing-link and missing-onset reasons. No actor was promoted from structure alone.

The repaired initial frontier contains 21 newly generated actor-local gaps across seven scenarios: seven each for `DEPENDENCY_HEALTH`, `FAILURE_ONSET`, and `RESOURCE_PRESSURE`. Each was generated because the resolver audit explicitly lacked onset-capable initiating evidence. The bounded mappings are:

| Gap dimension | Authorized capability | Target scope | Missing fact the observation can answer |
|---|---|---|---|
| `FAILURE_ONSET` | `events` | Exact unresolved Pod actor(s) | Actor-local event/onset evidence |
| `DEPENDENCY_HEALTH` | `runtime_traces` | Exact unresolved Pod actor(s) | Actor-local runtime boundary evidence |
| `RESOURCE_PRESSURE` | `resource_pressure` | Exact unresolved Pod actor(s) | Actor-local resource-pressure evidence |

When multiple hypotheses share a dimension and rationale, their gap carries the union of their hypothesis IDs and authorized target queries; each query remains bound to a target represented by a hypothesis in that gap. The 21 new gaps were not generated by the unresolved causal-role/incoming-edge path. Gaps from `NO_CAUSAL_SYMPTOM_LINK` alone were not assigned unsupported actor-local observations; the separate discovery phase guard keeps event/change discovery available while source linkage remains unestablished. No ground truth was used to construct or rank these gaps.

### Scenario-14

At initial diagnosis, all 13 hypotheses have `UNLINKED` source evidence, none is `SUPPORTED`, and none has a causal path. The old control eliminated all 13 through `NO_CAUSAL_SYMPTOM_LINK`; the repaired diagnosis leaves all 13 unresolved. With no supported hypothesis and unresolved alternatives, `INSUFFICIENT_EVIDENCE` is the truthful initial result. This is `EXPECTED_TRUTHFUL_ABSTENTION`, not an enum regression.

The raw `7e41b11` run remained `INSUFFICIENT_EVIDENCE` at the end because its phase-selection defect stopped the incident discovery path. With the generic phase repair, the investigation again follows the discovery sequence; final resolution is `AMBIGUOUS` with `ConfigMap/otel-demo/flagd-config`, matching the historical control. The final `AMBIGUOUS` → `INSUFFICIENT_EVIDENCE` change in run B was therefore a secondary phase defect, not a consequence required by the epistemic repair.

### Grader comparison after prediction sealing

The official grader was invoked only after each run's full prediction seal had been verified. Across baseline A and corrected run C:

| Measure | A: `c4da0cc` | C: `279fb96` | Change |
|---|---:|---:|---:|
| Initial correct root actors | 2/25 | 2/25 | 0 |
| Final correct root actors | 10/25 | 10/25 | 0 |
| Recovery | 8 | 8 | 0 |
| Harm | 0 | 0 | 0 |
| Initial/final actor identity changes | — | 0/25 | 0 |
| Initial/final resolution changes | — | 0/25 | 0 |

The corrected run's final outcomes are 8 `RECOVERY`, 2 `STABLE_CORRECT`, 15 `STABLE_WRONG`, and 0 `HARM`. Its M14-v1 investigation totals are 150 tool calls, 23 duplicate/already-known calls (15.3%), 36 decision-relevant calls (24.0%), 8 recoveries and 0 harm. The baseline and corrected run have the same decision-relevant and duplicate counts; novel evidence refs differ by three (1,384 vs 1,387) because the two remaining Scenario-6 reads differ.

G16 safety checks were applied mechanically and are prerequisite diagnostics, not a claim that M16's overall gates have been completed:

| Check | Corrected run C | Result |
|---|---:|---|
| G16.8 false `RESOLVED` on abstention/insufficient evidence | 0; no run produced any `RESOLVED` result | PASS for this prerequisite comparison |
| G16.9 wrong `RESOLVED` actor | 0; no run produced any `RESOLVED` result | PASS for this prerequisite comparison |
| G16.10 previously-correct leading actor removed from repaired causal competition without positive exclusion | 0/25 | PASS for this prerequisite comparison |

G16.10 was defined before grading: an answer actor present in baseline leading/plausible/unresolved/root competition regresses only if absent from repaired competition without positive evidence justifying exclusion. A move from `SUPPORTED` to `UNRESOLVED` is reported as unresolved, not as contradiction or elimination. The raw phase-defective run B had eight such competition regressions; corrected run C has zero. No baseline-correct root actor was lost or changed.

### Regression tests and validation

The prerequisite implementation is committed in `7e41b11` and the generic discovery-phase correction in `279fb96`. Tests were updated to encode the corrected epistemic behavior: an unlinked evidence-backed alternative remains unresolved and can keep a diagnosis ambiguous; two positively supported independent changes remain supported while an unlinked recorder hypothesis remains unresolved; CLI/report tests no longer expect an unsupported `RESOLVED` result.

- Focused resolution/intent/causal-lab/CLI tests: 58 passed.
- `make check`: PASS — Ruff, formatting (292 files), mypy (207 source files), pytest (873 passed; one existing Starlette deprecation warning).
- `make precommit`: PASS.
- `make rbac-check`: PASS; read-only control-plane permissions confirmed.
- M18A live validation was not rerun because this repair changed only RCA state interpretation, investigation phase selection and tests; it did not change runtime acquisition or normalization.
- Provider/model calls: 0. Kubernetes/application writes: 0. Secret access: 0. Out-of-policy execution: 0. Autonomous remediation: 0.

The sealed historical M15 artifact is unchanged. M15 action identity remains a historical control and is not represented as 150/150 after this epistemic repair: corrected run C is 148/150 exact, with both differences confined to the documented Scenario-6 unresolved-frontier sequence.

### Status

M16 Task 1: COMPLETE.  
M16 prerequisite — Repair missing-proof vs contradiction semantics: COMPLETE.  
M16 Task 2: NOT_STARTED.  
No `m16.v1` amendment was required. No M16 positive-elimination behavior was implemented.
