# M21 Supported-Leader Contract

Contract version: `m21-leader.v0`
Status: **MEASURED — FAILED / NOT AUTHORIZED FOR IMPLEMENTATION** (owner, 2026-09-28). Frozen before measurement (§8); the rule text below is unchanged by the result (§11).
Relation to `m21.v1`: this is a separate contract. It changes no clause of `m21.v1`, `m16.v1` or `m18a.v1`. `RESOLVED` keeps its exact current meaning.

## 1. The question this contract answers

> When the evidence positively supports exactly one symptom-linked root candidate, and every remaining rival lacks any modeled link to this incident's symptoms, how may the product say so — without claiming more than `RESOLVED`'s strict standard allows?

`RESOLVED` requires zero root-eligible `UNRESOLVED` alternatives. "Missing proof does not exclude" (m16, `m21.v1` I1) is correct and stays. The consequence is that in real, noisy clusters `RESOLVED` is practically unreachable. Rivals whose influence can never be proven absent always remain, for example uninstrumented harness, recorder or infrastructure Pods.

This contract adds a **separate, explicitly weaker** outcome. It is not an elimination and not a resolution. It lets the product state a leader honestly, together with the named residual rivals it could not exclude.

## 2. Measured problem (M20.6 / M21 feasibility, 35 ITBench-Lite scenarios)

- The correct leader already ranks first in 25–26/35 (engine 1.3.0). Correct `RESOLVED` is 0/35 and wrong `RESOLVED` is 0/35.
- The binding rival in 29/35 scenarios is the "R cohort", a `SUPPORTED` initiated change (recorder objects). Its `causal_explanation` is `PATH`, but its **`linked_symptoms` is empty**: the path comes from its grouped members, not from the incident's alert-derived symptom entities. The true root in the same scenarios has a non-empty `linked_symptoms`.
- Topic A (`m21.v1` §5.1) cannot exclude that rival in any scenario (0/35). The recorder Pods emit no spans (channel R/M uncovered), the alert provenance is unknown (O) and they share nodes with symptom Pods (N). The coverage bar is correct under I1/I2 and is not relaxed here.

## 3. Outcome and placement

**Decision (OD-L1):** the resolution stays `AMBIGUOUS`. The trace gains a separate, decision-bearing **leader assessment** with its own `decision_basis`:

```text
resolution          AMBIGUOUS                        (unchanged)
leader_assessment   SUPPORTED_LEADER | NONE
leader              the single symptom-anchored supported hypothesis
residual_rivals     every other root-eligible rival, each with a closed-set reason
```

Rationale: `RESOLVED`'s meaning, every existing metric and every consumer that treats `RESOLVED` as authority stay untouched. The alternative, a new `Resolution` value, is listed in §9 as OD-L1(b).

## 4. Semantics: `m21.supported-leader.v1`

**Definitions.**
- **Symptom-anchored support.** A hypothesis H is *symptom-anchored supported* iff all of these hold:
  - its epistemic state is `SUPPORTED`, so D1 `m21.support.change-onset-path.v1` is `FIRED` and there is no contradiction;
  - it is root-eligible;
  - its `linked_symptoms` is non-empty. That is, a modeled causal relation reaches at least one **alert-derived symptom entity** of this incident (`context.symptom_entities`), not only a grouped member's entity.
- **Symptom-linked rival.** A root-eligible hypothesis whose `linked_symptoms` is non-empty, whatever its epistemic state.
- **Residual rival.** A root-eligible hypothesis other than the leader, with **no** modeled link to any alert-derived symptom entity (`linked_symptoms` empty).

**Rule.** `SUPPORTED_LEADER` fires iff **all** of these hold:
1. The causal onset is `ANCHORED` (amendment 4) and alert coverage is `CONTIGUOUS`.
2. Exactly one hypothesis L is symptom-anchored supported.
3. No other root-eligible hypothesis is a symptom-linked rival, whether `SUPPORTED`, `UNRESOLVED` or anything else. A rival that reaches the symptoms, even without initiating evidence (for example an unsupported configuration root), **blocks** the rule.
4. Every other root-eligible hypothesis is a residual rival, recorded with its reason.
5. L carries no temporal contradiction, mechanism mismatch or ended-episode record, and is not a propagated effect.
6. The resolution is `AMBIGUOUS`, so the rule never applies on top of `RESOLVED` and never replaces it.

Otherwise the outcome is `NONE`, with the first failed condition as its reason. When an input needed to evaluate the rule is missing (for example, no audit for a hypothesis), the rule is `INAPPLICABLE` (`m21.v1` I4).

**Residual reasons (closed set, v1).**
- `NO_SYMPTOM_LINK_SUPPORTED`: supported, but its support links only to non-symptom entities (the R cohort shape).
- `NO_SYMPTOM_LINK_UNRESOLVED`: unresolved and unlinked (the C1, C2 and C4 shapes when they are unlinked).

A residual reason describes the **absence of modeled linkage**. It never asserts non-causality (I1).

## 5. What this outcome is not

- **Not an elimination.** Residual rivals keep their root eligibility and their epistemic state. No `ResolutionElimination` is produced (I9, I13).
- **Not `RESOLVED`.** No consumer may treat `SUPPORTED_LEADER` as `RESOLVED`. That covers remediation approval, auto-actions, benchmark "correct" counts and SLO reporting.
- **Not ranking.** Scores, ranks and verification confidence are not inputs (I4).
- **Not name-, namespace- or truth-aware.** Only typed structural fields are used: `linked_symptoms`, epistemic state, eligibility, onset and coverage status (I3).

## 6. Known risk (stated, not hidden)

The rule trusts the **modeled relation set**. A residual rival might in fact influence the symptoms through a relation the extractor does not model, for example an uninstrumented call or a shared node. The outcome's wording must therefore always say "leader under the modeled relations; the named residuals are not excluded". This risk is the reason the outcome sits under `AMBIGUOUS` and not under `RESOLVED`.

## 7. Audit, digest and replay

- **Record.** `LeaderAssessment` carries:
  - `rule_id`, `rule_version` and `status` (`FIRED` / `NOT_FIRED` / `INAPPLICABLE`);
  - `leader_hypothesis_id`;
  - `residual_rivals` as (hypothesis id, reason) pairs;
  - the first failed condition;
  - the decisive evidence ids (the leader's D1 decisive ids).

  All of it is structured; text is secondary (I8).
- **Digest.** The assessment is decision-bearing: it is shown to users as a claim. Its status and leader enter the epistemic digest, so replay verifies it.
- **Truncation.** The rule is evaluated over **all** hypotheses, not over the audit list truncated to 8, and the record lists all residual rivals.
- **Engine version.** `RCA_ENGINE_VERSION` gets a minor bump, because the digest changes wherever the rule fires. Earlier runs replay as UNSUPPORTED.

## 8. Safety gate and measurement protocol

This contract is frozen and committed **before** any measurement. Its result is reported as measured; the rule is never adjusted to pass.

Scoreboard on the 35 public scenarios (development data):
- **Hard gates.**
  - Wrong `RESOLVED`: 0 (unchanged).
  - **`SUPPORTED_LEADER` naming a non-ground-truth leader: 0.** A single wrong leader fails the gate.
  - Replay reproduces the digest.
  - No existing digest changes where the rule does not fire.
- **Reported.**
  - `SUPPORTED_LEADER` correct, N/35.
  - `NONE`, with its first failed condition.
  - The residual-reason distribution.
  - The R-cohort share of residuals.

Required negative controls (synthetic):
- (a) Two symptom-anchored supported changes → `NONE`.
- (b) An unsupported configuration root that is symptom-linked plus a supported unrelated change → `NONE`. The rival blocks.
- (c) Causal onset UNKNOWN → `INAPPLICABLE`/`NONE`.
- (d) A leader that is a propagated effect → `NONE`.
- (e) Removing `linked_symptoms` from the residual test flips the rule to firing on the R cohort shape. The mutation must be caught.
- (f) A `RESOLVED` diagnosis → the rule does not apply.

## 9. Owner decisions (closed 2026-09-28)

- **OD-L1: placement.**
  - **APPROVED: (a)** `AMBIGUOUS` plus a leader assessment.
  - Rejected: (b) a new `Resolution` value, which every consumer and metric would then have to handle.
- **OD-L2: blocking rivals. APPROVED:** any symptom-linked root-eligible rival blocks, even without initiating evidence. The alternative is to block only on supported rivals; it is more permissive and riskier.
- **OD-L3: remediation authority. APPROVED:** the same as `AMBIGUOUS`, meaning proposals only and no escalation.
- **OD-L4: user-facing wording. APPROVED:** it must name the residual rivals and state that they are not excluded ("leader under the modeled relations; the named residuals are not excluded").
- **OD-L5: relation to D1.1.** D1's `PATH` can come from grouped members; this contract keys on `linked_symptoms` instead. Should D1's own predicate be tightened later (D1.1)? **APPROVED:** that is a separate contract; this one does not depend on it.

## 10. Amendment record

| Date | Change | Source |
|---|---|---|
| 2026-09-28 | `m21-leader.v0` draft frozen as `m21-leader.v1`; OD-L1(a), OD-L2 (any symptom-linked rival blocks), OD-L3 (AMBIGUOUS authority), OD-L4 (wording), OD-L5 (D1.1 separate) approved as proposed. | Owner approval of the draft |
| 2026-09-28 | Measured under the frozen rule (§11): hard gate FAILED. Owner decision: no implementation; no grader change; no immediate v2. The owner designates this frozen contract `m21-leader.v0` (it was committed with the header `m21-leader.v1`); the rule id `m21.supported-leader.v1` and every clause are unchanged. | Owner decision on the measurement |

Amendment rule: as in `m21.v1` §8. If implementation or measurement conflicts with a clause, it stops with `CONTRACT_AMENDMENT_REQUIRED`; the contract is never changed to pass its gate.

## 11. Measurement outcome (2026-09-28)

Frozen rule (commit `bdea766`), engine on `main` `9d1bb1b` (A4 + D1), 35 ITBench-Lite scenarios, read-only (`.local/m20-audit/m21_leader/`):

```text
fired: 7/35
correct under current grader identity: 5/7   (S18, S19, S22, S35, S91)
incorrect: S25, S29
hard gate: FAILED
production implementation: NOT AUTHORIZED
```

- **S25, S29: root/fault identity disagreement requiring an independently specified identity model.** In both, the leader is a chaos fault object on the service the ground truth names. S25: the leader is a `StressChaos` experiment, while the ground truth is a `Schedule`. S29: the leader is a `JVMChaos` whose name does not match the ground-truth filter. The evidence holds no name-blind, persisted provenance that the generated chaos object and the scheduled root are the same causal root family, so no equivalence is assumed.
- **NONE (28).**
  - Symptom-linked rival: 14. The rivals are application Pods and Deployments; being symptom-linked does not prove they are only manifestations, so blocking here is the intended fail-closed behavior (OD-L2).
  - No leader: 12.
  - Two or more leaders: 2.
  - Not `AMBIGUOUS`: 5.
- Wrong `RESOLVED`: 0.

**Decision.**
- **No implementation.**
- **No change to the grader's matcher** for these cases.
- **No immediate v2** designed against the same 35 scenarios.

The same frozen `v0` rule will be re-measured, unchanged, after the rest of M21 (Topic A, D2 and the existing eligibility rules), because a different engine may present a different blocker set. Re-measuring an unchanged rule on an improved engine is not post-hoc tuning. If `wrong leader = 0` then, the rule becomes a production candidate again. If identity disagreements such as S25/S29 remain, they call for a separate causal root / fault-family identity contract, serving the grader, resolver, reporting and remediation alike — not a benchmark-specific patch.

