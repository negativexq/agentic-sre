# M21 Timing-Stability Contract (amendment 6)

Contract `m21-timing.v1`. **Status: IMPLEMENTED for snapshot sources (2026-09-29), with amendment 6.1
(three-valued relations, §4).** Commits `51d0853` (onset set), `459d2ef` (assessed-onset assembly),
`6f906df` (stability assessment), `f8fda87` (withheld authority). Live and replay sources are not yet
covered (§10).
It adds to [M21 v3](m21-causal-semantics-contract.md) and leaves §10.2 (the point onset H0, W, replay
of W) unchanged. Fault-execution rules (`m21.support.fault-execution.v1`) consume this contract; they
are not defined here.

## 1. Basis and non-goals

Measured on the 35 dev scenarios (read-only shadow runs, `.local/causal-closure/onset-shadow/`;
dev data, not a target): shifting only the onset changed temporal roles of 22–56% of matched claims,
the diagnosis status of 18–27% of shifted scenarios, and re-typed claims (42 `FAULT_INJECTION`
formations). `hypothesis_key` matched 0/56 claims across two onsets. The problem is the authority a
single onset point carries, not which point is right.

Non-goals: no persistence count, no minute window, no ground truth, no replacement of H0. §10.1 **VI**
(a fixed 5-minute `[L, U]`) was rejected for measured authority loss; this contract differs: the set is
evidence-derived, H0 stays the onset of record, and stability only gates *stronger* authority (§5), with
authority loss reported (§8) instead of tuned.

## 2. Three separate concepts

| Concept | Definition | Must not depend on |
|---|---|---|
| Incident onset uncertainty | The set `O` of admissible onset values (§3) | claim identity |
| Claim identity | `(actor, exact instance UID or explicit UID-unknown, mechanism, lifecycle episode)` | any incident onset, alert or symptom |
| Evidence temporal relation | `role(e, o)` of an evidence time against one onset value `o` (existing rules) | claim identity |

**I1 (identity invariance).** Changing the incident onset never changes a claim's identity; it may
change whether the claim forms and how it is evaluated. The *lifecycle episode* is the actor instance's own
episode (for example one chaos-experiment UID, one Schedule incarnation UID, one object UID). Two UIDs
with the same name are two claims. `hypothesis_key` is derived only from the identity tuple, with a family
made of the kinds of the actor's own cause-capable findings whatever temporal role the onset gives them
(`mechanism_family`; the assessed `mechanism` stays a separate, onset-dependent result). An exact UID that
is not observed is carried as `unknown`; nothing is inferred from names or other observations. Evidence
stamped with a foreign incident episode remains a separate evidence partition and keeps its own episode in
its key.

**I2 (causal family).** `causal_family_id` is derived from the logical actor and `mechanism_family` only
(never UID, onset, temporal role, score or evidence ids). Exact claims are never merged: UID, lifecycle,
evidence, timing, support and contradiction stay separate audit records. Root-cause competition is counted
per family: SUPPORTED if any selectable member is, else UNRESOLVED if any is, else EXCLUDED. A family also
reports `instance_resolution` (`EXACT`, `MULTIPLE_VIABLE`, `UNKNOWN`). Family aggregation grants no strong
authority and does not bypass the stability rules below; it only stops incarnations of one causal
proposition from competing with themselves.

## 3. Uncertainty set `O` (evidence-derived, diagnosis-time)

Inputs are alert captures with capture time `<=` the revision cutoff `C`, and W (§10.2).
- `H0` = the §10.2 onset. If it is UNKNOWN, `O` is empty and every outcome is `TIMING_UNASSESSED`.
- `U` = the earliest qualified episode start that is still firing in the last capture `<= C`; if none is
  firing, the latest qualified start `<= C`.
- `O` = the start times of all qualified (non-background, non-pre-existing) episodes `s` with `H0 <= s <= U`.

No count, duration or value participates. Each member records its derivation (alert fingerprint,
`activeAt`, capture evidence ids). An implementation may evaluate only one representative per interval
between evidence-role breakpoints, but its outputs must equal evaluating every member of `O`. (The current
implementation evaluates every member, reusing the onset-independent part of the case.)

## 4. Stability outcomes (two independent notions)

`TIMING_STABLE`, `TIMING_SENSITIVE`, `TIMING_UNASSESSED`, assessed separately for:
- **Formation stability** of a claim: the same identity forms with the same evidence set and mechanism
  under every `o` in `O`. Absence or re-typing under some `o` is `TIMING_SENSITIVE`.
- **Adjudication stability** of each relation a decision relied on. A relation takes one value per onset.

**Amendment 6.1 — relation values are three-valued (2026-09-29).** A relation that a rule could not even
attempt under some onset is `UNASSESSABLE`; it is not "did not hold". `UNASSESSABLE` never contradicts any
other value. A relation is `TIMING_SENSITIVE` only when it took two different *assessed* values, is
`TIMING_STABLE` when all assessed values agree, and is `TIMING_UNASSESSED` when it was never assessable.
The audit keeps every observed value.

Reason: the ended-episode rule needs observations after onset plus grace. Under a later admissible onset
the snapshot often ends too soon (`PARTIAL_COVERAGE_AFTER_DEADLINE`, `NO_DATA_AFTER_DEADLINE`), so the rule
is blocked, not contradicted; counting that as instability withheld 131 of 153 such eliminations (§8).

Relations that carry authority, and the only ones a stability gate reads: `temporal_contradiction`,
`ended_episode`, `d1`, `execution` (the observed quota rejection), and the claim's formation.
`onset_relation` (the categorical before/near/late label) is recorded for audit and is never a gate.

## 5. Authority rules

1. `TIMING_SENSITIVE` never removes a claim, its admission, or its possible-cause support. Tiers are
   still computed at H0 and carry a stability annotation.
2. Authority stronger than possible cause that relies on temporal ordering (mechanism-verified /
   `RESOLVED`, fault-execution support, positive causal roles) requires stability of the claim's
   formation, `d1` and `execution` relations. Otherwise it is not granted; the claim stays possible-cause.
3. Temporal *negative* authority (`m16.temporal-contradiction`, `m16.ended-manifestation-episode`)
   also requires adjudication stability of that relation. A sensitive elimination does not eliminate;
   the claim stays unresolved (`UNRESOLVED`, audit and reasons kept). This rule can reduce elimination
   authority; §8 reports it.
4. Nothing is upgraded because some other member of `O` would have been stronger.
5. Nothing is withheld when the onset is not assessed (`TIMING_UNASSESSED`).

Every withheld authority is recorded on the assessment (`withheld`: claim key, actor, authority kind,
relations) and enters the digest.

## 6. Diagnosis-time causality

`O` and every outcome use only evidence with observation time `<= C` of that revision. Later evidence
(later persistence, a later capture) creates a new revision and never rewrites an earlier one.
Investigation overlays that add evidence are new revisions (each re-derives the same evidence, including
its investigation findings, against every admissible onset). Replay recomputes the same `O` for the same `C`.

## 7. Digest, replay, versions

The epistemic digest includes: `C`, W, the qualified episodes considered (fingerprint, `activeAt`,
capture evidence ids), `H0`, `U`, the members of `O`, every formation/adjudication outcome and the withheld
authority, so replay can audit why it produced the same set. The digest is identical across processes with
different hash seeds. The engine version was **not** bumped by these changes (2.1.0 is the still-unreleased
series and a test pins it); recorded 2.1.0 runs made before them will not match, and a version bump is an
owner decision before release. Stored `hypothesis_key` values migrate by explicit remap or are marked
superseded, never silently reused.

## 8. Gates and measured results (35 dev scenarios, snapshot diagnosis; dev data, not a target)

| Gate | Result |
|---|---|
| 1. Determinism | Same digest under `PYTHONHASHSEED` 0 and 1 (three scenarios, maskless and masked); timing block identical across runs (35/35). |
| 2. Identity | Only the onset shifted ±8 min (17 scenarios): 634/636 (+8) and 626/636 (−8) `hypothesis_key`s shared, 0 duplicate or empty keys, actor and UID never changed. The rest are claims that form or vanish (`TRAFFIC_INCREASE`, `Pod/MANIFESTATION_ONLY`): formation sensitivity, not identity. |
| 3. No new strong authority | Strong authority 0 → 0 (none existed to withhold); possible-cause support 88 → 88; diagnosis status and resolution state identical in 35/35. |
| 4. Authority loss | Eliminations 200 → 127 (temporal 38 → 5, ended-episode 153 → 113); unresolved claims 128 → 150. 73 authorities withheld (33 temporal, 40 ended-episode) in 16/35 scenarios. Before amendment 6.1: 200 → 36 (82% loss), 164 withheld in 25/35. |
| 5. Usefulness | Over all 998 claims: formation 83% stable; relations sensitive: `d1` 7%, `temporal_contradiction` 3%, `ended_episode` 14% (before 6.1), `onset_relation` 35% (audit only). Over the *exercised* authority the picture differs: 33 of 38 temporal eliminations (87%) are sensitive. That is genuine timing sensitivity (the onset of record is often a one-snapshot alert), so temporal negative authority is nearly unusable on this data. This is reported, not tuned; the definition of `O` was not changed. |
| 6. Placebo | Identity is unchanged under arbitrary shifts (gate 2 measurement). Decision sensitivity to arbitrary shifts is in `.local/causal-closure/onset-shadow/tools/`. |
| 7. Scenario-21 | The 7 provisional temporal eliminations are withheld (temporal 7 → 0, unresolved 3 → 10, ended-episode 6 → 6 restored). |

The usefulness reading that first passed (all-claims) was misleading for authority that is actually
exercised; the exercised-authority view is what produced amendment 6.1. Any future usefulness report
must be conditioned on the claims where the relation is exercised.

## 9. Owner decisions (recorded)

- Membership of `O` as specified in §3: accepted.
- Rule 5.3, a sensitive temporal elimination leaves the claim unresolved: accepted.
- Rule 5.1, `SUPPORTED_CAUSE` stays an annotated tier, not downgraded by sensitivity: accepted.
- Amendment 6.1, three-valued relations: accepted 2026-09-29, before the re-measurement in §8.

## 10. Not yet covered

- **Live and replay sources** do not expose capture history, so their timing is `UNASSESSED` and nothing is
  withheld. Making stored revisions reproduce the same `O` requires persisting the qualified episodes with
  the run boundary.
- **Cost:** every diagnosis re-derives the case once per member of `O` (median 4, up to 37; a 36-member
  snapshot takes minutes). Evaluating one representative per role-breakpoint interval is allowed by §3 and
  not implemented.
- **Active investigation** reassesses on every turn. Measured 2026-09-29 (full regression and offline
  replay of all 35 scenarios, timing on): replay 35/35 PASS with source and transition parity true;
  snapshot and active status distributions identical to the run before timing; queries 210 → 210 and open
  material frontier 637 → 637; unresolved claims 128 → 150 (the same +22 as the snapshot measurement);
  decision-changing queries 47 → 49 (one more each in the two largest onset sets, Scenario-20 and
  Scenario-102); recorded source calls 525,543 → 581,700 (+11%, at most 28,641 per scenario); regression
  wall time about 30 minutes with 4 workers.
- **Formation stability** is reported and gates strong authority, but no rule yet consumes it beyond §5.2.
- Console and web do not display stability, withheld authority or `instance_resolution`.
