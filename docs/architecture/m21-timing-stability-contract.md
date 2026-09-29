# M21 Timing-Stability Contract (amendment 6, DRAFT)

Contract `m21-timing.v1`. **Status: DRAFT, not frozen. No production code changes with this text.**
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
with the same name are two claims. `hypothesis_key` is derived only from the identity tuple.

## 3. Uncertainty set `O` (evidence-derived, diagnosis-time)

Inputs are alert captures with capture time `<=` the revision cutoff `C`, and W (§10.2).
- `H0` = the §10.2 onset. If it is UNKNOWN, `O` is empty and every outcome is `TIMING_UNASSESSED`.
- `U` = the earliest qualified episode start that is still firing in the last capture `<= C`; if none is
  firing, the latest qualified start `<= C`.
- `O` = the start times of all qualified (non-background, non-pre-existing) episodes `s` with `H0 <= s <= U`.

No count, duration or value participates. Each member records its derivation (alert fingerprint,
`activeAt`, capture evidence ids). An implementation may evaluate only one representative per interval
between evidence-role breakpoints, but its outputs must equal evaluating every member of `O`.

## 4. Stability outcomes (two independent notions)

`TIMING_STABLE`, `TIMING_SENSITIVE`, `TIMING_UNASSESSED`, assessed separately for:
- **Formation stability** of a claim: the same identity forms with the same evidence set and mechanism
  under every `o` in `O`. Absence or re-typing under some `o` is `TIMING_SENSITIVE`.
- **Adjudication stability** of each temporal relation a decision relied on (initiating-before-onset,
  near-onset, temporal contradiction, ended-before-onset): the relation holds identically for every `o`
  in `O`. A decision is stable only if every relied-on relation is stable.

## 5. Authority rules

1. `TIMING_SENSITIVE` never removes a claim, its admission, or its possible-cause support. Tiers are
   still computed at H0 and carry a stability annotation (including the tiers `O` would give).
2. Authority stronger than possible cause that relies on temporal ordering (mechanism-verified /
   `RESOLVED`, fault-execution support, positive causal roles) requires formation and adjudication
   stability of everything it relied on. Otherwise it is not granted; the claim stays possible-cause.
3. Temporal *negative* authority (`m16.temporal-contradiction`, `m16.ended-manifestation-episode`)
   also requires adjudication stability. A sensitive elimination does not eliminate; the claim stays
   unresolved. This rule can reduce elimination authority; §8 reports it.
4. Nothing is upgraded because some other member of `O` would have been stronger.

## 6. Diagnosis-time causality

`O` and every outcome use only evidence with observation time `<= C` of that revision. Later evidence
(later persistence, a later capture) creates a new revision and never rewrites an earlier one.
Investigation overlays that add evidence are new revisions. Replay recomputes the same `O` for the same `C`.

## 7. Digest, replay, versions

The epistemic digest includes: `C`, W, the qualified episodes considered (fingerprint, `activeAt`,
capture evidence ids), `H0`, `U`, the members of `O`, and every formation/adjudication outcome, so replay
can audit why it produced the same set. New semantics id `m21.timing.v1`, engine minor version bump.
Revisions recorded before it replay as UNSUPPORTED, never as equivalent (as in §10.2). Stored
`hypothesis_key` values migrate by explicit remap or are marked superseded, never silently reused.

## 8. Gates (frozen before implementation, reported not tuned)

1. **Determinism:** same revision gives identical `O`, outcomes and digest.
2. **Identity:** with only the onset changed, claim identities match 100% (synthetic cases plus the shadow harness).
3. **No new strong authority:** strong or negative temporal authority never increases; the counts are reported.
4. **Authority loss report:** temporal eliminations and strong authorities before and after; `SUPPORTED_CAUSE`
   cases with their stability annotation.
5. **Usefulness report:** share of `TIMING_STABLE`, `TIMING_SENSITIVE`, `TIMING_UNASSESSED` per relation.
   If almost everything is sensitive, that is reported as FAIL of the definition of `O`, and `O` is revisited;
   no number is tuned to pass.
6. **Placebo:** arbitrary onset shifts create no identity change.
7. **Mandatory regression case, Scenario-21.** Checkpoint `5bb46c4` (instance UIDs and schedule
   lifecycles) made real experiment start times visible, and the existing temporal rule then produced 7
   `EXPLICIT_TEMPORAL_CONTRADICTION` eliminations (0 before) against an onset that is a one-snapshot alert
   (17:37:06); the first persistent episode starts near 17:53. These are **provisional**: identity and
   lifecycle correctness is accepted, the temporal authority they implied is not endorsed. When the
   stability implementation lands, re-evaluate these 7 eliminations; under rule 5.3 they must not eliminate
   unless the relation is `TIMING_STABLE` over `O`.

## 9. Owner decisions needed

- Membership of `O` (all qualified starts in `[H0, U]` as above, or narrower).
- Rule 5.3: a sensitive temporal elimination leaves the claim unresolved.
- Rule 5.1: `SUPPORTED_CAUSE` stays an annotated tier, not downgraded by sensitivity.
