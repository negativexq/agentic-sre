# M19 Product Resolution Protocol

Status: **ACTIVE**

## 1. Purpose

This protocol describes the active M19 DEV validation scenarios and evidence
rules. It describes how a diagnosis may move from `AMBIGUOUS` to `RESOLVED`
when qualifying positive evidence is captured, persisted, and evaluated by the
deterministic RCA rules. Scenario outcomes are evaluated at diagnosis revisions
and against the evidence available to each revision.

This document transcribes the M19 plan's existing semantics. True-root
selections are recorded in §4 under M19-0.3.

DEV scenarios are project-authored validation scenarios; their results are not
reported as blind, pre-registered, or as evidence of generalization.

## 2. Scope

The protocol covers the active DEV scenarios PR-01, PR-01N, PR-02, PR-03,
and PR-03N; PR-02N is superseded by the owner decision below. It also covers
the R1, R_early, and R2 revision timeline; evidence membership and replay;
decisive evidence; T1–T4 and N0–N3 (N2 is superseded, not passed); global
product safety; and replay-level T5/T6 ablation checks.

The M19 product objective is read-only observation, continuous evidence
collection, evidence continuity, exact entity-instance identity, diagnosis
revisions, evidence-driven reevaluation, neutral handling of missing evidence,
positive-evidence elimination, deterministic offline replay, and preservation
of legacy correctness. Chaos Mesh, dirty-cluster and concurrent-incident
validation, topology history, and capability/degraded mode are outside scope.

## 3. Binding Rules

The following M19 plan rules bind this protocol:

1. **Truth blindness.** The prediction path must not use scenario IDs,
   expectations, protocol root actors, scenario-specific namespace/name
   constants, or grader labels. Scenario- or expectation-keyed production
   logic is prohibited.
2. **M16 rules.** A1 (`m16.ended-manifestation-episode`, version `v1`)
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
6. **Legacy suite.** The existing 25 scenario IDs, expectations, labels, and
   `Tier` values do not change. The legacy runner retains its truncate behavior
   and is marked `legacy`.
7. **Contract conflict.** A material conflict with a frozen contract is
   reported as `CONTRACT_AMENDMENT_REQUIRED`, including the clause, current
   behavior, counter-evidence, minimal amendment proposal, and affected gate;
   work stops.
8. **Scope discipline.** M19 admits work only for a frozen-contract conflict,
   a gate that cannot be mechanically proven, implementation impossibility,
   or a fix required for a working product. New features, UX, extra experiments,
   benchmark-result improvements, and unrelated refactors do not enter M19.

## 4. Scenario Catalog

DEV timelines use `T0` for the true-root action, `onset` for the engine's
calculated onset, and a 15-minute `grace`. Root actions and actors qualified
under M19-0.3 are recorded with their source scenario and evidence below.

### 4.1 PR-01

**Purpose and candidate hypotheses:** Lifecycle convergence using A1
`TERMINATED` and `RECOVERED`. `H_root` is the true-root hypothesis. `H_A` is a
manifestation hypothesis for Pod A; `H_B` is a manifestation hypothesis for
Pod B.

**Timeline:** T−25m, `SetReadiness(payment, 40s)` creates unhealthy Pod A. T−23m,
`DeletePodOf(payment)` deletes Pod A. T−10m, `SetReadiness(order, 40s)` makes Pod B
unhealthy and then Ready again on the same UID. At T0, apply the qualified
true-root action and start sustained traffic.

**True root action:**
`EnvPatch(deployment="payment-service", values={"FAULT_PAYMENT_DELAY_MS": "10000"}, wait=True)`.

**Qualified source scenario:** `payment_config_change`.

**Qualified actor:** `sre-demo/Deployment/payment-service`.

**Qualified causal mechanism/class:** `["SPEC_CHANGE"]`.

**Qualification evidence:** Q causal hypothesis `hypothesis:07031ff119a87d11` is
`SUPPORTED`; Q result is `CORRECT` and binding is `QUALIFIABLE`.

- Frozen scenario source: `b982c40:packages/evals/live/scenarios.py`.
- Historical G16: `docs/results/m16-positive-elimination.md`, Task 2; `.local/live-bench/m16-b982c40/g16-rows.json` (`payment_config_change`, `outcome_new=CORRECT`).
- Qualification evidence type: M19 frozen-baseline requalification.
- Qualification artifact: `.local/m19-requalification/b982c40/scenarios/payment_config_change.json`.
- Requalification manifest SHA-256: `13be19826a66121a5ceb2d24115c793c174362e0dc2c8676d785bcc1158ef96b`.

**R1:** `AMBIGUOUS`; `H_root` is `SUPPORTED`; `H_A` is root-ineligible by
`TERMINATED` for uid A; `H_B` remains plausible; `STATUS_CONTINUITY` is `OPEN`.

**R_early:** `H_B` has not been eliminated.

**R2:** `H_B` is root-ineligible by `RECOVERED` for uid B; resolution is
`RESOLVED`; the leader is the protocol root actor selected under M19-0.3.

**Proof:** T1–T4 and N0.

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

**CPU-normal calibration (M19-7.1, owner-frozen 2026-09-27):** `order-service`
`requests.cpu = 100m` (unchanged), `limits.cpu = 200m`, under 2 req/s traffic
through the order-service Service DNS. Throttle peaks observed with the product's
A2 reader on three independent fresh product clusters: 0.0123, 0.0034, 0.0037;
acceptance max peak 0.0123 ≤ 0.10. One further run was an infrastructure failure
before measuring (§1.9 retry), not a measurement.

**True root action:**
`EnvPatch(deployment="payment-service", values={"FAULT_PAYMENT_DELAY_MS": "3000"}, wait=True)`.

**Qualified source scenario:** `payment_config_cross_service_impact`.

**Qualified actor:** `sre-demo/Deployment/payment-service`.

**Qualified causal mechanism/class:** `["SPEC_CHANGE"]`.

**Qualification evidence:** Q causal hypothesis `hypothesis:19b2ef9869fe364a` is
`SUPPORTED`; Q result is `CORRECT` and binding is `QUALIFIABLE`.

- Frozen scenario source: `b982c40:packages/evals/live/scenarios.py`.
- Historical G16: `docs/results/m16-positive-elimination.md`, Task 2; `.local/live-bench/m16-b982c40/g16-rows.json` (`payment_config_cross_service_impact`, `outcome_new=CORRECT`).
- Qualification evidence type: M19 frozen-baseline requalification.
- Qualification artifact: `.local/m19-requalification/b982c40/scenarios/payment_config_cross_service_impact.json`.
- Requalification manifest SHA-256: `13be19826a66121a5ceb2d24115c793c174362e0dc2c8676d785bcc1158ef96b`.

**R1:** `AMBIGUOUS`; `H_cpu` is plausible and `RESOURCE_COVERAGE` is `OPEN`.

**R_early:** `H_cpu` is not contradicted.

**R2:** With complete normal coverage, `H_cpu` is `CONTRADICTED` under
`m16.resource-pressure` version `v1`; resolution is `RESOLVED`, with the
protocol root actor as leader.

**Proof:** T1–T4 and N0.

### 4.4 PR-02N — SUPERSEDED

**Owner decision (2026-09-27):** Synthetic CPU-pressure calibration is
abandoned. The frozen 100m/100m candidate measured a raw throttle peak of
0.029289 against the ≥ 0.40 target. No CPU-burn fault, traffic increase,
request change, or threshold tuning will be added. Future development
prioritizes real RCA capability and real incident evidence over tuning
synthetic pressure scenarios to meet benchmark thresholds. PR-02N is not a
passing negative proof and is no longer an active scenario.

### 4.5 PR-03

**Purpose and candidate hypotheses:** A2 memory resource-pressure candidate
elimination with a true-root actor and mechanism different from PR-02.
`H_mem` is the candidate hypothesis; `H_root` is the true-root hypothesis.

**Timeline:** At T−3m, lower `order` memory limits as a resource-only change.
At T0, apply the separately qualified true-root action.

**True root action:**
`PatchService(name="payment-service", spec={"selector": {"app": "payment-service-retired"}})`.

**Qualified source scenario:** `service_selector_drift`.

**Qualified actor:** `sre-demo/Service/payment-service`.

**Qualified causal mechanism/class:** `["SPEC_CHANGE"]`.

**Qualification evidence:** Q causal hypothesis `hypothesis:4ed1dfb085f9268e` is
`SUPPORTED`; Q result is `CORRECT` and binding is `QUALIFIABLE`.

- Frozen scenario source: `b982c40:packages/evals/live/scenarios.py`.
- Historical G16: `docs/results/m16-positive-elimination.md`, Task 2; `.local/live-bench/m16-b982c40/g16-rows.json` (`service_selector_drift`, `outcome_new=CORRECT`).
- Qualification evidence type: M19 frozen-baseline requalification.
- Qualification artifact: `.local/m19-requalification/b982c40/scenarios/service_selector_drift.json`.
- Requalification manifest SHA-256: `13be19826a66121a5ceb2d24115c793c174362e0dc2c8676d785bcc1158ef96b`.

Qualification provenance note: Historical M16 G16 preserved scenario-level
correctness but did not preserve the complete SUPPORTED-hypothesis-to-actor/
mechanism binding required by M19-0.3. The binding used here comes from the
separately labeled M19 frozen-baseline requalification run on exact code and
scenario revision `b982c40`. Historical M16 G16 results are not modified or
reinterpreted.

**R1:** `AMBIGUOUS`; `H_mem` is plausible.

**R_early:** `H_mem` is not contradicted.

**R2:** With complete normal memory coverage and peak below 0.9, `H_mem` is
`CONTRADICTED`; resolution is `RESOLVED`, with the protocol root actor as
leader.

**Proof:** T1–T4 and N0.

### 4.6 PR-03N

**Purpose and candidate hypotheses:** Negative memory case with actual memory
pressure produced by ballast using the M19-7.4 calibration. `H_mem` is the
candidate hypothesis.

**Timeline:** Apply the calibrated ballast that produces actual memory
pressure. Other scenario timing follows the PR-03 revision sequence.

**R1 / R_early / R2:** The candidate is not contradicted by A2 normality.

**Proof:** N3.

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
`H_x` and qualified protocol root actor.

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

R2 is `RESOLVED`, and its leader is the protocol root actor at the exact
`namespace/Kind/name` identity.

### T5 — Evidence ablation (replay fixture check)

This deterministic offline replay ablation is tested on replay fixtures by
M19-7.P and is not part of live DEV scenario acceptance. It uses the complete
recorded R2 manifest, tape, and normalized investigation Findings. The planner
does not run and no new provider call is made. Replay supplies the recorded
responses while evidence records whose IDs are in `D` are filtered, then
reruns `diagnose_case`. If one Finding contains both `D` and non-`D` evidence
and cannot be safely filtered, the result is `ABLATION_AMBIGUOUS` and the proof
fails. `H_x` must no longer be eliminated by the target result, and the
resulting diagnosis must not be `RESOLVED`.

### T6 — Rule ablation (replay fixture check)

This deterministic offline replay ablation is tested on replay fixtures by
M19-7.P and is not part of live DEV scenario acceptance. It uses the complete
recorded R2 evidence and disables only the target rule through an
evaluation-only override. The T6 rule-ablation override does not enter the
production config; it is included in the ablation's own deterministic
eval/replay config digest, and the product artifact `code.config_digest` does
not change. No production `EngineConfig`, API, environment or control-plane
surface can disable a rule. The ablated diagnosis must not be `RESOLVED`.

T2 bir novelty gate'idir, causal necessity gate'i değildir; causal necessity T5/T6 ile test edilir.

T5/T6 hedef kural ve kanıtın final resolution için necessary contributor olduğunu kanıtlar; tek başına sufficient olduğunu iddia etmez.

## 9. Negative Proofs

### N0 — Genuine early revision

At approximately onset+8m, the persisted `MANUAL` R_early has no target
elimination, `D ∩ U(R_early) = ∅`, and no `RESOLVED` result based on the target
rule.

### N1 — PR-01N

`H_B` is not root-ineligible by `RECOVERED`; its continuity precondition is
`DISQUALIFIED`.

### N2 — PR-02N — SUPERSEDED

PR-02N was not executed as a negative proof. Its synthetic CPU-pressure
calibration was abandoned by owner decision; no N2 PASS is claimed.

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

## 14. Review Acceptance Policy

M19 scope admits work only for:

1. a conflict with a frozen contract;
2. a gate that cannot be mechanically proven;
3. implementation impossibility; or
4. a fix required for a working product.

New features, UX, extra experiments, benchmark-result improvements, and
unrelated refactors do not enter M19.

## 15. Protocol Amendment Procedure

For a material conflict with a frozen M16/M18 contract, report
`CONTRACT_AMENDMENT_REQUIRED` with the affected clause, current behavior,
counter-evidence, minimal amendment proposal, and affected gate, then stop
work. Scope discipline remains as stated in §14.

Not specified beyond the binding M19 review/STOP rules.
