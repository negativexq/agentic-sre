# M19 Product Resolution Protocol

## 1. Purpose

This protocol defines the pre-registered product-resolution scenarios and
evidence rules for M19. It describes how a diagnosis may move from `AMBIGUOUS`
to `RESOLVED` when qualifying positive evidence is captured, persisted, and
evaluated by the deterministic RCA rules. Scenario outcomes are evaluated at
diagnosis revisions and against the evidence available to each revision.

This document transcribes the M19 plan's existing semantics. It does not select
the true-root mechanisms; those remain subject to M19-0.3 qualification.

## 2. Scope

The protocol covers six DEV scenarios: PR-01, PR-01N, PR-02, PR-02N, PR-03,
and PR-03N; the R1, R_early, and R2 revision timeline; evidence membership and
replay; decisive evidence; T1–T7 and N0–N3; global product safety; and the blind
HOLDOUT procedure. HOLDOUT scenario and expectation contents are not included.

The M19 product objective is read-only observation, continuous evidence
collection, evidence continuity, exact entity-instance identity, diagnosis
revisions, evidence-driven reevaluation, neutral handling of missing evidence,
positive-evidence elimination, deterministic offline replay, and preservation
of legacy correctness. Chaos Mesh, dirty-cluster and concurrent-incident
validation, topology history, and capability/degraded mode are outside scope.

## 3. Binding Rules

The following M19 plan rules bind this protocol:

1. **Truth blindness.** The prediction path must not use scenario IDs,
   expectations, sealed root actors, scenario-specific namespace/name
   constants, HOLDOUT parameters, or grader labels. Scenario-keyed prediction
   logic is prohibited.
2. **Frozen M16 rules.** A1 (`m16.ended-manifestation-episode`, version `v1`)
   and A2 (`m16.resource-pressure`, version `v1`) retain their thresholds and
   positive-elimination semantics. They may be narrowed, not broadened. A
   required broadening triggers `CONTRACT_AMENDMENT_REQUIRED` and a stop.
3. **Authority boundary.** Adapters, tape, capture, manifest construction,
   normalizers, planners, scheduler, harness, and LLM do not decide hypothesis
   state, elimination, or resolution. The scheduler starts a diagnosis run;
   deterministic RCA rules make diagnosis decisions.
4. **Missing is not normal.** `NO_DATA`, `UNKNOWN`, partial coverage, missing
   UID or timestamp, provider errors, reads absent from the tape, and collector
   gaps do not establish contradiction, elimination, root ineligibility, or
   health.
5. **Append-only authoritative evidence.** Product paths do not update, delete,
   or truncate authoritative evidence. Materialized indexes are not temporal
   evidence and cannot supply RCA or replay history.
6. **Frozen legacy suite.** The existing 25 scenario IDs, expectations, labels,
   and `Tier` values do not change. The legacy runner retains its truncate
   behavior and is marked `legacy`.
7. **Contract conflict.** A material conflict with a frozen contract is
   reported as `CONTRACT_AMENDMENT_REQUIRED`, including the clause, current
   behavior, counter-evidence, minimal amendment proposal, and affected gate;
   work stops.
8. **HOLDOUT isolation.** HOLDOUT parameters and expectations remain hidden
   from the agent until Phase 8. This protocol carries only their SHA-256
   values. After code freeze, the owner mounts the scenario file read-only for
   the runner and the expectation file read-only for the separate grader.
9. **Review acceptance.** After protocol seal, scope may be reopened only for a
   frozen-contract conflict, a gate that cannot be mechanically proven, or
   implementation impossibility. Feature, UX, refactor, and result-improvement
   suggestions do not enter M19.

## 4. Scenario Catalog

DEV timelines use `T0` for the true-root action, `onset` for the engine's
calculated onset, and a 15-minute `grace`. Root actions and actors that require
M19-0.3 qualification remain explicit placeholders here.

### 4.1 PR-01

**Purpose and candidate hypotheses:** Lifecycle convergence using A1
`TERMINATED` and `RECOVERED`. `H_root` is the true-root hypothesis. `H_A` is a
manifestation hypothesis for Pod A; `H_B` is a manifestation hypothesis for
Pod B.

**Timeline:** T−25m, `SetReadiness(payment, 40s)` creates unhealthy Pod A. T−23m,
`DeletePodOf(payment)` deletes Pod A. T−10m, `SetReadiness(order, 40s)` makes Pod B
unhealthy and then Ready again on the same UID. At T0, apply the qualified
true-root action and start sustained traffic.

**True root:** `TBD — M19-0.3 qualified true-root selection`.

**R1:** `AMBIGUOUS`; `H_root` is `SUPPORTED`; `H_A` is root-ineligible by
`TERMINATED` for uid A; `H_B` remains plausible; `STATUS_CONTINUITY` is `OPEN`.

**R_early:** `H_B` has not been eliminated.

**R2:** `H_B` is root-ineligible by `RECOVERED` for uid B; resolution is
`RESOLVED`; the leader is the sealed actor selected under M19-0.3.

**Proof:** T1–T7 and N0.

### 4.2 PR-01N

**Purpose and candidate hypotheses:** Negative continuity case paired with
PR-01. `H_B` is the candidate manifestation hypothesis.

**Timeline:** Run the PR-01 timeline and add `SetReadiness(order, 30s)` at
T0+4m.

**R1 / R_early / R2:** Use the PR-01 revision sequence. At R2, the continuity
precondition is `DISQUALIFIED`; `H_B` is not root-ineligible by `RECOVERED`.

**Proof:** N1.

### 4.3 PR-02

**Purpose and candidate hypotheses:** A2 CPU resource-pressure candidate
elimination. `H_cpu` is the candidate hypothesis; `H_root` is the true-root
hypothesis.

**Timeline:** At T−3m, apply a resource-only change to `order` CPU limits using
the M19-7.1 normal-calibration value. At T0, apply a qualified payment-side
true-root action.

**True root:** `TBD — M19-0.3 qualified true-root selection`.

**R1:** `AMBIGUOUS`; `H_cpu` is plausible and `RESOURCE_COVERAGE` is `OPEN`.

**R_early:** `H_cpu` is not contradicted.

**R2:** With complete normal coverage, `H_cpu` is `CONTRADICTED` under
`m16.resource-pressure` version `v1`; resolution is `RESOLVED`, with the sealed
true-root actor as leader.

**Proof:** T1–T7 and N0.

### 4.4 PR-02N

**Purpose and candidate hypotheses:** Negative CPU case in which the CPU limit
produces actual pressure using the M19-7.2 calibration value. `H_cpu` remains
the candidate hypothesis.

**Timeline:** Apply the calibrated pressure-producing CPU limit as the resource
change. Other scenario timing follows the PR-02 revision sequence.

**R1 / R_early / R2:** The candidate is not contradicted by A2 normality.

**Proof:** N2.

### 4.5 PR-03

**Purpose and candidate hypotheses:** A2 memory resource-pressure candidate
elimination with a true-root actor and mechanism different from PR-02.
`H_mem` is the candidate hypothesis; `H_root` is the true-root hypothesis.

**Timeline:** At T−3m, lower `order` memory limits as a resource-only change.
At T0, apply the separately qualified true-root action.

**True root:** `TBD — M19-0.3 qualified true-root selection`.

**R1:** `AMBIGUOUS`; `H_mem` is plausible.

**R_early:** `H_mem` is not contradicted.

**R2:** With complete normal memory coverage and peak below 0.9, `H_mem` is
`CONTRADICTED`; resolution is `RESOLVED`, with the sealed true-root actor as
leader.

**Proof:** T1–T7 and N0.

### 4.6 PR-03N

**Purpose and candidate hypotheses:** Negative memory case with actual memory
pressure produced by ballast using the M19-7.4 calibration. `H_mem` is the
candidate hypothesis.

**Timeline:** Apply the calibrated ballast that produces actual memory
pressure. Other scenario timing follows the PR-03 revision sequence.

**R1 / R_early / R2:** The candidate is not contradicted by A2 normality.

**Proof:** N3.

### 4.7 HOLDOUT Scenarios

The following are placeholders only. Scenario parameters, root actors, and
expectations are withheld.

#### PR-01H

Scenario file SHA-256: TBD (M19-0.6)
Expectation file SHA-256: TBD (M19-0.6)

#### PR-02H

Scenario file SHA-256: TBD (M19-0.6)
Expectation file SHA-256: TBD (M19-0.6)

#### PR-03H

Scenario file SHA-256: TBD (M19-0.6)
Expectation file SHA-256: TBD (M19-0.6)

## 5. Diagnosis Revision Timeline

The revision sequence is `R1 = INITIAL`, `R_early = MANUAL`, and
`R2 = EVIDENCE_DEADLINE`. `R_early` is approximately onset+8m. `R2` is
approximately onset+15m+settle, after the evidence grace and scheduler settle
period.

Precondition outcomes are `PASS`, `PENDING(not_before)`, or
`DISQUALIFIED(reason)`. `PENDING` does not change hypothesis state. Missing or
partial evidence after the deadline is recorded as
`NO_DATA_AFTER_DEADLINE` or `PARTIAL_COVERAGE_AFTER_DEADLINE`; it is not
contradiction.

### 5.1 R1

`R1` is the initial diagnosis created by auto-diagnosis when the incident opens.
Its trigger is `INITIAL`. Its evidence is bounded by its own base evidence
manifest and committed provider tape. The expected starting resolution for
positive DEV scenarios is `AMBIGUOUS`.

### 5.2 R_early

`R_early` is a `MANUAL` revision requested by the harness at approximately
onset+8m. This is the harness's single POST in the revision schedule. It
records an early evidence boundary; for positive scenarios, the target
elimination is absent and its decisive evidence is not yet in `U(R_early)`.

### 5.3 R2

`R2` is an `EVIDENCE_DEADLINE` revision started by the scheduler after an open
evidence requirement reaches its deadline and settle period. The harness does
not POST R2; it waits and reads the resulting revision. The scheduler starts a
diagnosis run and has no RCA decision authority.

## 6. Evidence Universe

For a revision `Rn`, the run's evidence universe is bound to persisted
membership, not inferred from timestamps alone. The run follows this sequence:

```text
CAPTURE
  ↓
COMMIT
  ↓
BASE EVIDENCE MANIFEST
  ↓
RCA / INVESTIGATION
  ↓
Diagnosis Revision
```

### 6.1 Base Evidence Manifest

The base manifest records the exact source IDs visible to the run from its
authoritative evidence sources, together with the run's snapshot-cycle
membership. The planned sources are alerts, object versions, event versions,
lifecycle observations, change records, log observations, and the snapshot
cycle. The manifest is formed after capture commits. Its `manifest_digest` is
the SHA-256 of the sorted `source_type:source_id` entries.

### 6.2 Snapshot Cycle

The diagnosis capture includes a Kubernetes object listing whose full object
bodies, including status, are persisted as the run's snapshot cycle. A
completed scope and a failed scope remain distinguishable. A cycle without a
diagnosis `run_id` is not written as a diagnosis snapshot cycle. Snapshot
objects are part of the base evidence boundary used to construct the run's
source and replay its object history.

### 6.3 Provider Tape

Prometheus, Loki, and Tempo reads from capture, the engine, or investigation
belong to one ordered, run-local provider tape. Each response follows this
sequence:

```text
provider call
→ normalized response envelope
→ provider tape persistence
→ commit
→ caller consumes result
```

If persistence fails, the caller does not consume the result. Provider errors
are retained as errors and treated as missing evidence by RCA; they do not
become normal observations. Reads from an earlier run do not enter the current
run's evidence.

### 6.4 Normalized Investigation Evidence

Investigation observations are normalized into persisted evidence and typed
Findings before they can affect deterministic RCA. The evidence universe for a
revision includes its manifest, its committed provider-tape evidence, and its
normalized investigation evidence. `NO_DATA`, `UNKNOWN`, and partial coverage
remain missing or incomplete evidence, not proof that a candidate is normal or
contradicted.

## 7. Decisive Evidence

For a target elimination, `D` is the elimination's `decisive_evidence_ids`:
the complete set of evidence that satisfies its decisive positive
precondition, not a sample of that evidence.

- **A1 TERMINATED:** all qualifying `DELETED` evidence for the exact UID.
- **A1 RECOVERED:** all persisted observations for that same UID that satisfy
  `observed ≥ onset + grace`, `Ready=True`, `ready_since ≤ onset`, and the
  continuity requirement.
- **A2:** every required `PodCoverage` evidence item for each
  `pod × container × resource`.

For revision `Rn`, `U(Rn)` is that revision's base manifest plus its committed
provider-tape evidence and normalized investigation evidence.

## 8. Positive Transition Proofs

These predicates apply to each positive DEV scenario's target hypothesis
`H_x` and qualified sealed actor.

### T1 — Genuine ambiguity

R1 is `AMBIGUOUS` and has at least two plausible hypotheses, including `H_x`.
A plausible hypothesis is not `CONTRADICTED` and is root-eligible.

### T2 — Evidence novelty

`D` is non-empty, `D ∩ U(R1) = ∅`, and `D ⊆ U(R2)`. The comparison is about
evidence membership; time comparison is informational only.

### T3 — Correct rule and binding

The `H_x` elimination uses the expected `rule_id` and `rule_version`; all
preconditions pass; targets are exact; and the UID/resource binding is valid.

### T4 — Correct resolution

R2 is `RESOLVED`, and its leader is the sealed actor at the exact
`namespace/Kind/name` identity.

### T5 — Evidence ablation

This is a resolver-level ablation using the R2 manifest, R2 tape, and normalized
investigation Findings. The planner does not run and no new provider call is
made. Replay supplies the recorded responses while evidence records whose IDs
are in `D` are filtered. If one Finding contains both `D` and non-`D` evidence
and cannot be safely filtered, the result is `ABLATION_AMBIGUOUS` and the proof
fails. Re-run `diagnose_case`. `H_x` must no longer be eliminated by the target
result, and the resulting diagnosis must not be `RESOLVED`.

### T6 — Rule ablation

Use the complete R2 evidence and disable only the target rule through an
evaluation-only configuration, included in `config_digest` and unavailable in
production. The resulting diagnosis must not be `RESOLVED`.

### T7 — Truth blindness

Before code freeze, the agent did not have access to HOLDOUT contents. During
prediction, the prediction process could not see the expectation file.

## 9. Negative Proofs

### N0 — Genuine early revision

At approximately onset+8m, the persisted `MANUAL` R_early has no target
elimination, `D ∩ U(R_early) = ∅`, and no `RESOLVED` result based on the target
rule.

### N1 — PR-01N

`H_B` is not root-ineligible by `RECOVERED`; its continuity precondition is
`DISQUALIFIED`.

### N2 — PR-02N

`H_cpu` is not contradicted by A2 normality.

### N3 — PR-03N

`H_mem` is not contradicted by A2 normality.

## 10. Global Product Safety

All global product safety counters are zero:

```text
false RESOLVED = 0
wrong RESOLVED actor = 0
UID misbinding = 0
missing → contradiction = 0
fabricated evidence = 0
tape divergence = 0
unresolved tape evidence id = 0
synthetic cluster evidence = 0
```

## 11. Replay Semantics

Replay uses the persisted base evidence manifest, snapshot cycle, provider tape,
and normalized persisted evidence, together with the recorded trajectory or a
deterministic selector and the engine/config identity. It must reproduce the
same epistemic digest without access to live external systems.

```text
Kubernetes calls = 0
Prometheus calls = 0
Loki calls = 0
Tempo calls = 0
trajectory replay LLM calls = 0
```

Trajectory replay uses the recorded action sequence. Selector replay reruns the
deterministic observation/intent selector against the tape; its selected query
sequence is compared with the recorded sequence. A tape mismatch is a replay
divergence, not `NO_DATA`; a recorded provider error replays as an error.

## 12. Evidence Ablation Semantics

Evidence ablation is the resolver-level T5 procedure. It uses the complete R2
inputs without running the planner or making new provider calls, filters the
records identified by `D`, and runs `diagnose_case`. `H_x` must remain
uneliminated by the target result and the ablated diagnosis must not be
`RESOLVED`. An inseparable Finding containing both `D` and non-`D` evidence
produces `ABLATION_AMBIGUOUS` and fails the proof.

## 13. Rule Ablation Semantics

Rule ablation is the T6 procedure. It uses the complete R2 evidence and an
evaluation-only configuration that disables only the target rule. This
configuration contributes to `config_digest` and is not available in
production. The ablated diagnosis must not be `RESOLVED`.

## 14. Blind HOLDOUT Procedure

HOLDOUT scenario parameters and expectations are hidden from the agent until
Phase 8. Before then, this protocol carries only their SHA-256 values. The
placeholders above remain until M19-0.6 supplies the hashes; no HOLDOUT
plaintext belongs in this document or the repository.

After code freeze, the owner mounts the scenario file and expectation file
read-only outside the repository and agent workspace. The runner reads the
scenario file only. The prediction process cannot see the expectation file; a
separate grader evaluates the artifact against that file. Each HOLDOUT scenario
is run once, with retries only for the infrastructure failures and within the
M19 retry limit.

If HOLDOUT fails, the same revealed set is not rerun after code changes. The
gate is reported as failed; a new attempt requires a protocol amendment and a
new sealed HOLDOUT.

## 15. Review Acceptance Policy

After protocol seal, M19 scope may be reopened only for:

1. a conflict with a frozen contract;
2. a gate that cannot be mechanically proven; or
3. implementation impossibility.

Feature, UX, refactor, or benchmark-result improvement requests do not enter
M19.

## 16. Protocol Amendment Procedure

For a material conflict with a frozen contract, report
`CONTRACT_AMENDMENT_REQUIRED` with the affected clause, current behavior,
counter-evidence, minimal amendment proposal, and affected gate, then stop work.
Protocol scope may be reopened only under the review acceptance conditions in
§15. A HOLDOUT failure requires a protocol amendment and a new sealed HOLDOUT
before a new attempt.

Not specified beyond the binding M19 review/STOP rules.
