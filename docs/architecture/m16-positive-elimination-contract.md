# M16 Positive Alternative Elimination Contract

Contract version: `m16.v1`
Status: **FROZEN**
Baseline implementation HEAD: `b2cb0b22f6e02909bef17ea51e9d419214f3ca8c`
Scope: deterministic RCA consequences for positive evidence only. This contract does not authorize M16 production implementation by itself.

## 1. Objective and authority boundary

M18A answers **what was observed**. M16 answers **what deterministic RCA consequence that evidence justifies**.

The permitted sequence is:

```text
bounded evidence
→ typed observation
→ deterministic Finding
→ deterministic M16 evidence rule
→ hypothesis consequence
→ existing resolution machinery
```

The adapter acquires data. The normalizer makes typed observations and Findings. Only deterministic RCA consequence code may change epistemic state, record an elimination, exclude an actor from root-cause competition, or produce a resolution. The planner selects bounded reads but has no RCA authority. An LLM has no elimination authority.

Forbidden authority paths include raw telemetry, a normalizer, an investigation tool, a planner, candidate rank, or an LLM directly declaring support, contradiction, elimination, root cause, or `RESOLVED`.

## 2. Audited current authority path

The audited baseline is the code at the HEAD above. The principal path is:

```text
ObservationSource
→ engine.build_case()
→ deterministic signal Findings / runtime evidence / propagation
→ group_candidates() and Hypothesis
→ assess_hypothesis() and root-cause eligibility
→ resolve_hypotheses()
→ Diagnosis
```

Investigation normalization appends typed Findings and rebuilds the same deterministic case. The normalizer does not directly call the resolver or write epistemic state.

| Authority path | Source / input | Positive evidence and preconditions | Hypothesis / resolution consequence | Audit and M16 disposition |
|---|---|---|---|---|
| `NO_CAUSAL_SYMPTOM_LINK` | `resolution.assess_hypothesis`; `Hypothesis.causal_explanation` | Current code checks only that the explanation is `PATH` or `DIRECT`; `UNLINKED` may mean no currently derived path/link | Immediately sets `CONTRADICTED`; `_elimination_for()` emits this code unless a hard temporal contradiction takes precedence | Evidence IDs are all hypothesis Findings. **M16_MUST_NOT_REUSE** as positive exclusion. Pre-existing defect; see §5. |
| `EXPLICIT_TEMPORAL_CONTRADICTION` | `hypotheses._make_hypothesis` populates `contradictory_findings`; `temporal.temporal_contradiction_certainty`; `resolution.assess_hypothesis` | A contradiction Finding is positively present and `DEFINITELY_LATE` under onset + configured grace; interval uncertainty is not hard | Sets that hypothesis `CONTRADICTED`; may allow a competing supported hypothesis to resolve | `HypothesisResolutionAudit.contradictory_evidence_ids` identifies the contradiction evidence; elimination record currently stores all episode evidence. **KEEP_AS_IS** for the existing temporal rule; M16 must strengthen its structured audit. |
| `NO_ONSET_CAPABLE_INITIATING_EVIDENCE` | `resolution.assess_hypothesis`; actor/episode Findings | No actor-aligned initiating Finding in the episode | Adds a plausibility reason but, by itself, yields `UNRESOLVED`, not `CONTRADICTED`; it is not directly emitted as an elimination | Reason is in hypothesis audit. `_elimination_for()` has a detail string for this code, but its fallback code path is not reachable from this reason alone. **KEEP_AS_IS** as unresolved/missing proof. |
| Structural dominance | `resolution.dominates()` and `resolve_hypotheses()`; two `SUPPORTED` hypotheses | Both hypotheses are supported; weaker evidence-shape keys are a subset of stronger keys; stronger adds an onset-capable initiating evidence shape | A unique dominator can make resolution `RESOLVED`; the weaker hypothesis remains in `plausible_hypotheses` and is not added to `eliminated_hypotheses` | `DominanceRelation` records pair and bounded evidence IDs. `STRUCTURALLY_DOMINATED` enum is not emitted. **CLARIFY_ONLY**; do not relabel this as contradiction or positive counter-evidence. |
| `ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT` | `runtime_propagation` → `causal_roles` → `root_cause_eligibility` → resolver | Strict direct client/server parent-child trace relation; both endpoints positively non-success; exact actor binding verified from Kubernetes history; incoming propagation matched to the exact hypothesis actor; no source-capable initiating Finding anywhere in the hypothesis episode; mixed role evidence remains undetermined | Excludes the actor from root-cause competition while retaining the observed manifestation; may leave one eligible hypothesis and permit existing resolution | Eligibility includes initiating and propagation refs/bases; `ResolutionElimination` records those refs. **KEEP_AS_IS**; do not duplicate under a new M16 rule. |
| Verification predicates / confidence | `ranking.verification_trace()`, `verify()`, `engine._accept_override()` | Predicates such as linkage, initiating signal, timing and score are used for confidence and acceptance of an investigator-proposed candidate | Can change confidence or reject an investigator override; does not itself set hypothesis `CONTRADICTED`, populate eliminated IDs, or decide `Resolution` | `VerificationTrace` records predicates/evidence IDs. **M16_MUST_NOT_REUSE** as elimination authority. |
| Contradictory-Finding construction | `hypotheses._make_hypothesis()` from temporal roles and actor/kind | A Finding is consequence-timed and is on the causal actor, or is an initiating kind | Adds a candidate contradiction to the hypothesis; only later `assess_hypothesis()` decides hard-vs-uncertain timing | Finding evidence IDs remain on hypothesis and contradictory audit. **KEEP_AS_IS**, subject to temporal contract below. |
| Root-cause eligibility exclusion | `root_cause_eligibility.assess_root_cause_eligibility()` | Positive causal-role assessment plus verified incoming edge/pairs; propagated/manifestation role; no episode-level source-capable initiating evidence; mixed evidence prevents exclusion | `INELIGIBLE_PROPAGATED_EFFECT`; resolver removes it from eligible leader competition and records an elimination | Structured assessment carries basis, exact bounded refs and counts; elimination record is less detailed. **KEEP_AS_IS** and require richer M16 elimination audit. |
| `GapOutcomeKind.CONTRADICTS` consumption | `information_gap._outcomes()` and investigation graph/normalizers | No production path currently generates or consumes this outcome as hypothesis contradiction. `_outcomes()` emits `SUPPORTS`, `NO_DATA`, and `UNKNOWN`; normalizer maps typed evidence to `SUPPORTS` only for normalized Findings attributed to hypotheses, else `UNKNOWN` | No current resolver state transition consumes `GapOutcomeKind.CONTRADICTS` | Enum exists but is inert for RCA consequences. **M16_MUST_NOT_REUSE** as an implicit transition. |
| Structural alternative lifecycle | `frontier.apply_frontier_progress()`; `StructuralAlternative.status` | Actor is promoted only by deterministic hypothesis evidence; otherwise queried dimensions may mark a frontier `QUERIED_NO_CAUSAL_FINDING` | Changes exploration bookkeeping/status only; a structural alternative is not a hypothesis and is not added to resolver eliminations | Frontier progress has query/evidence audit at investigation layer. **M16_MUST_NOT_REUSE** as hypothesis exclusion. |

### Existing reason codes

| Code | Exact current use | Contract treatment |
|---|---|---|
| `NO_CAUSAL_SYMPTOM_LINK` | `UNLINKED` explanation causes `CONTRADICTED` in current resolver | Do not use as positive elimination until the pre-existing defect in §5 is repaired. |
| `NO_ONSET_CAPABLE_INITIATING_EVIDENCE` | Plausibility reason when no aligned initiating Finding exists; alone leaves state `UNRESOLVED` | Missing initiating proof is neutral/unresolved. Never turn this reason alone into contradiction. |
| `EXPLICIT_TEMPORAL_CONTRADICTION` | A positive contradictory Finding is definitely late beyond configured grace | Preserve only with temporal preconditions in §7. |
| `STRUCTURALLY_DOMINATED` | Declared in the enum; no current production emitter found | Do not claim it is an existing elimination rule. A future use needs its own explicit contract and audit. |
| `ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT` | Verified incoming propagation plus no episode initiating evidence excludes an actor from root competition | Preserve the distinction between root ineligibility and denying the observed effect. |

`ResolutionElimination` currently stores `hypothesis_id`, `code`, `evidence_ids`, and free-text `detail`. Temporal elimination currently receives all episode evidence IDs, not only the contradictory Finding IDs. The per-hypothesis audit separately retains `contradictory_evidence_ids`; root eligibility has a richer structured assessment. M16 must make the elimination record itself mechanically attributable to the exact rule and evidence.

## 3. Five distinct epistemic concepts

### Support

Positive observed evidence consistent with one hypothesis or mechanism. Support does not imply competing hypotheses are false and does not, by itself, resolve a competition.

### Counter-evidence

Positive observed evidence that weighs against a specific required property of a hypothesis under an explicit deterministic rule. Counter-evidence may leave the hypothesis `UNRESOLVED`; it is not automatically contradiction or elimination.

### Contradiction

Positive evidence logically incompatible with a required hypothesis property after all target, mechanism, time, direction, and completeness preconditions pass. A contradiction may set the hypothesis to `CONTRADICTED` only through deterministic RCA code.

### Root-cause ineligibility

Evidence that an actor is a positively established propagated effect or manifestation and is not eligible to compete as the initiating root under the exact rule. This does not deny that the actor failed or that the observation occurred.

### Elimination

A deterministic, audited consequence that removes a hypothesis from plausible root-cause competition. It requires a named rule and positive evidence. It is not equivalent to low score, low rank, absent evidence, counter-evidence, or root-cause ineligibility, though a separately named root-eligibility consequence can exclude an actor from root competition without claiming its episode is false.

## 4. Positive evidence and exclusion matrix

“Potential counter-evidence” below means a typed observation could be evaluated by a future M16 rule. It does not assert that the current resolver already has that rule. Unsupported or incomplete cases remain deferred.

| Hypothesis / mechanism class | Positive support available now | Potential valid positive exclusion / counter-evidence | Neutral or insufficient | Required target / time / direction / completeness | M16 disposition |
|---|---|---|---|---|---|
| `RESOURCE_PRESSURE` | Existing `RESOURCE_PRESSURE` Finding from deterministic threshold-crossing samples; M18A Prometheus can produce typed measured normal state | `OBSERVED_NORMAL` may counter only the exact resource mechanism required by the hypothesis | `NO_DATA`, `UNKNOWN`, partial coverage, no threshold Finding, or normal measurement of a different metric | Same Pod (or an explicitly justified exact binding); causal incident interval; named metric/rule; real sufficient coverage and baseline/reference required by that metric | M16 may define a specific observed-normal mismatch rule; no generic normal-state elimination. |
| `TRAFFIC_CHANGE` | Existing `TRAFFIC_INCREASE` Finding under the unchanged 1.5× baseline rule | Sufficiently covered `OBSERVED_NORMAL` traffic may counter a hypothesis that specifically requires an increase | Empty/partial series, absent baseline, no onset, or no traffic Finding without complete coverage | Same Service; interval spanning the required pre/post-onset comparison; real samples with coverage under the existing step rule; unchanged threshold | Candidate M16 rule only for the exact traffic-change predicate. |
| `DEPENDENCY_FAILURE` | Deterministically normalized `DEPENDENCY_ERRORS`; typed trace outcome/direction facts; existing propagation metadata | A positive, complete, exact-peer observation may counter only a narrowly stated dependency mechanism if a deterministic current fact actually demonstrates incompatibility | Unmatched log text, `NO_DATA`, `UNKNOWN`, a successful partial/best-effort trace search, or lack of a dependency Finding | Exact dependency peer/semantic relation; same relevant interval; typed recognized mechanism; direction and completeness sufficient for the claim | Generic healthy/no-error inference is deferred; do not treat query silence as dependency health. |
| `LOCAL_SERVICE_FAILURE` | Positive actor-bound local failure Finding or typed local span outcome, if an existing normalizer/rule represents it | Measured normal local execution paired with positive downstream abnormal evidence could counter a specifically local mechanism | A child error alone, generic service error, missing local span, or no local Finding | Exact local service/workload binding; relevant interval; distinguish local span from child duration/outcome; sufficient trace relation | Deferred unless the typed evidence identifies the exact local predicate and an existing deterministic consumer. |
| `PROPAGATED_DOWNSTREAM_FAILURE` | Existing strict direct trace propagation evidence | Existing `ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT` can exclude root competition under its episode rule | One downstream error span, caller/callee names alone, unresolved binding, mixed source/propagation evidence | Strict direct client/server parent-child pairing; explicit non-success at both endpoints for propagated return; direction from typed span relation; verified exact Kubernetes actor binding; no source-capable initiating evidence in episode | Reuse existing root eligibility; do not add a duplicate elimination. |
| `CONFIG / ROLLOUT CHANGE` | Existing typed change Findings and actor/topology grouping | An initiating change definitely observed after onset + grace can contradict a hypothesis that requires that exact change to initiate the incident | Missing timestamp, one-sided object-history interval, interval straddling boundary, late manifestation/restart not required by the hypothesis | Positive exact change Finding; causal time from `initiating_at`/`schedule_active_from` or `at`; object changes require previous/current observation interval; exact hypothesis actor/premise; fixed configured grace | Preserve `EXPLICIT_TEMPORAL_CONTRADICTION` only when all conditions pass. |
| `AUTOSCALING_FAILURE` | Existing `AUTOSCALING_FAILURE` Finding | No generic positive exclusion is currently admitted | No failure Finding, absent metrics, low rank, or no scaling event | Would require a positive observation of the exact autoscaling predicate and a current deterministic consumer | Deferred. |
| `NETWORK / POLICY MECHANISM` | Existing network restriction/policy Findings and topology relations | A future exclusion requires positive evidence proving the required route/policy condition false for the exact mechanism | Missing policy, no matching Finding, query absence, or unrelated policy state | Exact policy and endpoints; relevant time/version; explicit relation/semantics in deterministic code | Deferred absent a typed positive incompatible observation and current consumer. |
| Generic / unsupported mechanism | Only whatever typed Finding family an existing rule explicitly consumes | None by default | Any missing, empty, unknown, unrecognized, or unrelated data | Must be defined before implementation with exact actor, mechanism, time, and completeness | Deferred; absence never becomes exclusion. |

No matrix row authorizes a normalizer, adapter, or planner to change hypothesis state. Prometheus normal observations are typed observation state in M18A, not normal-state Findings. Loki unmatched text remains neutral. Tempo does not establish normality from incomplete search results.

## 5. Pre-existing implementation discrepancy

### `NO_CAUSAL_SYMPTOM_LINK` currently treats absent linkage as contradiction

`hypotheses._make_hypothesis()` sets `causal_explanation` to `UNLINKED` when it has no derived causal path and no linked symptom. `resolution.assess_hypothesis()` maps any explanation outside `PATH`/`DIRECT` to `NO_CAUSAL_SYMPTOM_LINK`, then sets the epistemic state to `CONTRADICTED` without requiring a positive incompatible observation. `resolve_hypotheses()` then includes the ID in `eliminated_hypotheses` and emits a `ResolutionElimination`.

This is a pre-existing semantic discrepancy: missing/undiscovered linkage can be indistinguishable from positively disproven linkage. It conflicts with the M16 invariant that missing evidence, absent paths, candidate absence, and low rank are not contradiction.

Classification: **BLOCKS_M16_IMPLEMENTATION**. M16 Task 2 must first repair or positively qualify this existing path before new positive-elimination rules rely on resolver states. Do not silently fix it in this contract-only task. This defect does not prevent freezing the contract because the contract records it as a prerequisite and explicitly forbids reuse of this path as positive evidence.

### Other audited discrepancies

| Finding | Classification | Required treatment |
|---|---|---|
| Temporal elimination record includes all hypothesis evidence, while exact contradictory refs are only on `HypothesisResolutionAudit` | NON_BLOCKING | M16 audit structures must carry exact evidence refs and rule provenance on each elimination. |
| `NO_ONSET_CAPABLE_INITIATING_EVIDENCE` has an `_elimination_for()` detail entry, but no-onset alone is `UNRESOLVED` and does not reach the elimination builder | DOCUMENTATION_ONLY | Preserve as missing proof; do not make it eliminative. Keep code-path fact visible in tests/audit. |
| `STRUCTURALLY_DOMINATED` reason code has no production emitter; dominance uses `DominanceRelation` and can resolve without changing hypothesis epistemic states | DOCUMENTATION_ONLY | Treat structural dominance as a distinct existing resolver discriminator, not as a positive-observation contradiction. Audit it explicitly. |

No contract amendment is required for `m18a.v1`. No production code was changed during this audit.

## 6. `OBSERVED_NORMAL` and positive mechanism mismatch

`OBSERVED_NORMAL` is scoped evidence that the tested predicate was measured in its rule-defined normal range. It is not universal health, and does not imply another metric, actor, dependency, interval, or mechanism was normal.

A future M16 rule may treat it as counter-evidence only if all applicable requirements hold:

1. The typed state was produced from actual usable measurements; `NO_DATA` and `UNKNOWN` are distinct states.
2. The exact target/binding matches the causal actor or mechanism predicate in the hypothesis.
3. The observation interval overlaps/covers the hypothesis-relevant incident interval required by the rule.
4. The metric/fact family tests the exact required mechanism, not a proxy chosen because it is available.
5. The deterministic coverage/completeness rule and named threshold/rule ID pass.
6. Provenance identifies the query descriptor, source observations, normalization rule, and relevant interval.
7. The observation is evaluated by deterministic RCA consequence logic; M18A components remain evidence-only.

Even then, counter-evidence may leave the hypothesis `UNRESOLVED`. Elimination requires a separate frozen rule proving incompatibility and satisfying the elimination audit contract. `OBSERVED_ABNORMAL` establishes only that the typed predicate was abnormal; it does not establish root cause.

## 7. Temporal exclusion contract

Temporal exclusion requires positive, timestamped evidence and a hypothesis that requires the exact observation to initiate the incident. The current shared helper uses `Finding.incident_onset`, the `RankingConfig.verification_onset_grace` (15 minutes by default), and `causal_time()` precedence: `details.initiating_at`, then `details.schedule_active_from`, then `Finding.at`.

For object changes in `CONFIG_CHANGE`, `SPEC_CHANGE`, `IMAGE_CHANGE`, `SCALE_CHANGE`, and `ROLLOUT_RESTART`, absent an explicit initiating/schedule time, current evidence is an interval bounded by `previous_observed_at` and `Finding.at`:

- if current observation is at/before onset + grace: `NOT_LATE`;
- if previous observation is after onset + grace: `DEFINITELY_LATE`;
- if interval crosses the boundary or either timestamp is missing: `INTERVAL_UNCERTAIN`/`UNKNOWN`.

For other Findings the deterministic point causal time is compared with the boundary. Only `DEFINITELY_LATE` is a hard temporal contradiction. Missing/ambiguous timestamps remain neutral or unresolved. A late symptom, manifestation, restart, or other consequence does not contradict an upstream initiating hypothesis unless that exact item is a required initiating premise of the hypothesis and the deterministic relation proves incompatibility.

The current hypothesis builder marks consequence-timed initiating-kind Findings as `contradictory_findings` (subject to actor/kind rules); the resolver independently checks the certainty. M16 must retain both the positive Finding requirement and certainty check, and its audit must capture the onset, causal timestamp or interval, grace, and evidence ID.

## 8. Mechanism mismatch contract

Mechanism mismatch is positive incompatibility, not missing support. It requires:

```text
positive typed observation
AND exact relevant target / semantic actor
AND exact mechanism dimension tested
AND compatible incident time scope
AND sufficient completeness / coverage
AND a deterministic rule proving incompatibility with a required hypothesis predicate
```

No matching Finding, empty result, absent telemetry, a low score/rank, or an unrelated healthy metric cannot establish mismatch. An explicit normal metric can only counter its own metric predicate. A normal caller with an abnormal child may distinguish local from downstream mechanism only when the trace fact contains typed caller/callee direction, outcomes, exact bindings, ordered timestamps, and the tested hypothesis actually requires local failure. It still does not name a root actor by itself.

## 9. Propagation and root-versus-symptom contract

Current propagation is derived from a canonical trace index and strict parent/child client-server span pairing. A propagation edge represents an observed remote non-success return; the model itself states that it does not identify the initiating cause. Runtime Kubernetes bindings are checked against object history at the span time. Only `VERIFIED` incoming bindings participate in the existing propagated-effect role used for root eligibility. Unresolved or contradicted bindings do not qualify. A hypothesis with both actor-local initiating evidence and verified incoming propagation is `UNKNOWN`/mixed for role purposes, not excluded.

The existing root-ineligibility consequence additionally requires propagated/manifestation role and no source-capable initiating Finding in the full hypothesis episode. It excludes the exact actor from root competition while preserving the observed episode. M16 must reuse this rule and its evidence rather than create a parallel “downstream means not root” rule.

Forbidden inferences include downstream error alone → downstream root, caller/callee relation alone → root assignment, incoming error alone → local actor is merely propagated, or an unverified service name → exact Kubernetes actor. Direction and causal ordering must come from typed parent/child semantics and positive timestamps/bindings, not arbitrary topology adjacency.

## 10. Neutral and insufficient evidence

The following are neutral with respect to hypothesis falsity and elimination:

```text
NO_DATA
UNKNOWN
missing evidence / Finding
missing timestamp or ambiguous temporal interval
partial or incomplete telemetry
unrecognized raw log text
low score or low rank
planner non-selection
candidate absence
unqueried or exhausted structural frontier
```

The state rules are:

| Evidence / state | Permitted consequence |
|---|---|
| `NO_DATA` | No hypothesis state change; no contradiction, elimination, normality, or health inference. |
| `UNKNOWN` | No hypothesis state change. |
| `OBSERVED_NORMAL` | No state change unless a specific positive scoped counter-evidence rule applies. |
| `OBSERVED_ABNORMAL` | Typed support/evidence only unless a specific deterministic incompatibility rule applies to a different required mechanism; never root cause by itself. |
| Counter-evidence | May remain `UNRESOLVED`; not necessarily contradiction or elimination. |
| Contradiction | May set `CONTRADICTED` only after its exact rule and evidence preconditions pass. |
| Root-cause ineligibility | May exclude root competition without denying observed manifestation. |
| Elimination | Must carry structured evidence, rule, scope, and deterministic consequence. |

## 11. Required M16 elimination audit contract

Each future elimination must be mechanically inspectable, not explained only by free text. The record must expose:

```text
hypothesis_id
reason_code / consequence kind
rule_id and rule_version
exact evidence_ids and, for runtime evidence, source observation_ids
semantic target(s) / actor(s)
mechanism or dimension tested
relevant timestamp(s) or bounded time interval
coverage/completeness basis where applicable
preconditions evaluated and their deterministic results
structured explanation of why the positive evidence satisfies the rule
```

Existing M18A provenance must remain intact: pillar, semantic capability, canonical target, requested/effective time, trusted query descriptor, source observation IDs, and normalization rule. M16 adds consequence provenance; it must not erase acquisition provenance.

The current `ResolutionElimination(hypothesis_id, code, evidence_ids, detail)` is insufficient by itself for these fields. A `HypothesisResolutionAudit`, `DominanceRelation`, and root-eligibility assessment may provide related context, but future audit consumers must be able to identify the specific rule and exact evidence directly from the elimination/consequence record. Structured fields take precedence over descriptive text.

### Reason-code contract

Preserve historical codes and meanings; do not rename them for convenience. Reuse `EXPLICIT_TEMPORAL_CONTRADICTION` only for the scoped temporal rule above and `ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT` only for existing positive propagated-effect root eligibility. Do not emit `NO_ONSET_CAPABLE_INITIATING_EVIDENCE` as contradiction/elimination.

Potential future codes, only if the current enum cannot express a concrete implemented rule, include:

```text
OBSERVED_NORMAL_MECHANISM_MISMATCH
POSITIVE_MECHANISM_CONTRADICTION
```

These are proposed names, not current codes and not implementation authorization. Prefer a specific rule code over a generic `CONTRADICTED` reason.

## 12. Deferred and unsupported rules

The following remain deferred until typed evidence and a current deterministic transition rule support them:

- generic service/dependency health from absence of logs or spans;
- resource or traffic normality without exact target, interval, named predicate, and sufficient coverage;
- latency introduction, critical path, first failing hop, or local-vs-child time claims without an implemented deterministic threshold/rule;
- Loki pool exhaustion, database acquisition delay, or other categories not represented by tested deterministic patterns;
- autoscaling or network/policy exclusion from absent observations;
- temporal contradiction from a missing timestamp, uncertain change interval, or late symptom;
- causal conclusions from topology alone, arbitrary neighbors, raw telemetry, or `GapOutcomeKind.CONTRADICTS`;
- elimination from evidence-shape dominance unless a distinct rule and complete audit explicitly justify that consequence.

## 13. G16.1–G16.10 evaluation semantics

These gates remain unchanged and are frozen as follows:

| Gate | Frozen interpretation |
|---|---|
| G16.1 evidence-only | Only deterministic RCA consequence logic changes hypothesis/elimination/resolution state; adapters, normalizers, tools, planner and LLM do not. |
| G16.2 NO_DATA neutrality | `NO_DATA` cannot change hypothesis state or imply normality, contradiction, elimination, or health. |
| G16.3 positive counter-evidence | At least one scoped, positive, typed counter-evidence path is demonstrated; it must be rule-backed and may remain unresolved. |
| G16.4 root vs propagated proof | Root-vs-effect distinction uses positive typed direction, time and exact actor binding; no downstream-error shortcut. Existing root-eligibility behavior is audited/reused. |
| G16.5 temporal exclusion | Timestamped positive evidence satisfies the exact initiating-hypothesis and onset-grace rule; uncertain intervals do not exclude. |
| G16.6 audit | Every new elimination includes structured reason, rule/version, exact evidence, semantic target, time/mechanism scope and precondition results. |
| G16.7 ≥3 genuine `AMBIGUOUS → RESOLVED` | Pre-state contains at least two plausible competing causal hypotheses; new positive evidence deterministically excludes one or more; that evidence causes the resolution transition. No threshold reduction, scenario deletion, denominator change, truth lookup, planner shortcut, or forced actor selection. |
| G16.8 false resolved on abstention/insufficient = 0 | Abstention, `NO_DATA`, `UNKNOWN`, missing/partial evidence, or insufficient evidence never yields a false `RESOLVED`. |
| G16.9 wrong resolved actor = 0 | No evaluated resolved result selects an actor different from the sealed evaluation answer; labels remain grader-only and unavailable during prediction. |
| G16.10 no previously-correct leading actor regression | Previously correct leading actors do not regress on the frozen comparison set; report resolution and actor changes separately. |

Do not change denominators, scenario IDs, confidence/resolution thresholds, or gate definitions to pass these gates.

## 14. Truth-blindness

Prediction-time RCA may use only the current scenario observations, typed source evidence, topology, runtime evidence, and deterministic case state available by that time. It must not read ground-truth root actors, grader labels, expected transitions, or benchmark answers. No rule may be keyed to a scenario ID or answer label. Ground truth remains evaluation-only.

## 15. Examples

### Positive counter-evidence candidate, not automatic elimination

```text
Hypothesis: Pod A resource pressure caused the incident.
Evidence: same Pod A; same causal window; sufficient real measured coverage;
          OBSERVED_NORMAL under the exact resource-pressure rule.
```

This may enter a deterministic, scoped resource-mechanism counter-evidence rule. It is not automatically an elimination; other episode evidence and the rule's exact preconditions still matter.

### Invalid absence inference

```text
Pod A resource query → NO_DATA
```

Consequence: neutral. No resource health, contradiction, or elimination.

### Valid temporal exclusion candidate

```text
Hypothesis requires a particular rollout as initiating cause.
The exact rollout interval is positively observed wholly after incident onset + grace.
```

Potential consequence: `EXPLICIT_TEMPORAL_CONTRADICTION`, if the hypothesis requires that rollout and the frozen timestamp/interval rule passes.

### Invalid late-symptom inference

```text
Pod restart or failure manifestation occurs after onset.
```

Consequence: does not automatically contradict an upstream initiating hypothesis.

### Propagation example

```text
A → B is a strict observed client/server parent-child pair.
Both endpoints have explicit non-success outcomes and exact bindings are verified.
```

This can support the existing typed propagation/root-eligibility rule when episode requirements also pass. It does not declare B to be the root.

### Invalid propagation shortcut

```text
B has one error span, or a topology edge A → B exists.
```

This alone establishes neither B as root nor A as non-root.

## 16. Pre-implementation prerequisite and amendment rule

Before M16 production implementation, the `NO_CAUSAL_SYMPTOM_LINK` discrepancy in §5 must be corrected or qualified with positive evidence so absence of a derived link cannot set `CONTRADICTED`. The repair must preserve truthful abstention and the existing thresholds. Task 2 must add regression coverage for unlinked/missing topology and for any genuinely positive no-link evidence if such a rule is claimed.

If implementation discovers a material conflict between this contract and the audited code/data semantics, stop and report `CONTRACT_AMENDMENT_REQUIRED` with the clause, contrary evidence, minimal proposed amendment, and gate impact. Do not silently weaken this contract, M18A, authorization, safety, or evaluation gates. Only a reviewed, separately recorded amendment may change `m16.v1`.

## 17. Amendment A1 — ended pre-onset manifestation episodes (`m16.v1-a1`)

Status: **FROZEN** (owner-approved 2026-09-24). This amendment adds exactly one root-ineligibility rule. Sections 1–16 are unchanged and remain binding; where this section is silent, they govern.

### Contrary evidence that required it (§16)

On the 25-scenario live suite at `da6f0e6`, all 14 `AMBIGUOUS` diagnoses have exactly one `SUPPORTED` hypothesis and 96 `UNRESOLVED` alternatives. 93 of the 96 are Pod hypotheses that contain only `FAILURE_EVENT`/`CONTAINER_FAILURE` Findings (readiness `Unhealthy`, `BackOff`), and all of their 100 Findings are timestamped before incident onset. Under §4 they cannot be excluded, so G16.7 is unreachable on that population. A truth-blind CLASS 0 replay of ITBench TEST25 at `45677c4` (`docs/results/m16-positive-elimination.md`, "Task 2 — G16.7 structural replay") shows the complementary picture: 23/25 final diagnoses have no `SUPPORTED` hypothesis at all, so this rule cannot create a `RESOLVED` result there.

### Rule `m16.ended-manifestation-episode.v1`

Reason code `MANIFESTATION_EPISODE_ENDED_BEFORE_ONSET`; consequence `ROOT_INELIGIBILITY` (§3), same machinery and audit contract (§11) as `ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT`. The hypothesis is excluded from root-cause competition for this incident episode; its epistemic state is not changed to `CONTRADICTED`.

All preconditions must pass, each recorded with its deterministic result:

1. **Pod actor.** The hypothesis' causal actor is a Pod.
2. **Manifestation-only.** The hypothesis has no initiating Findings, zero episode source-capable initiating Findings, and every Finding is a `FAILURE_EVENT` or `CONTAINER_FAILURE` on the causal actor itself. Any other Finding kind or entity makes the rule inapplicable.
3. **Timed manifestations.** Every Finding has a known time; `FAILURE_EVENT` uses the latest observed occurrence (`last_at`, else `first_at`). A missing timestamp makes the rule inapplicable.
4. **Positive episode end E**, exactly one of:
   - **TERMINATED** — an object-journal `DELETED` version of the exact Pod (same namespace/name and `metadata.uid`) whose `observed_at` is at or before incident onset. The deletion happened no later than `observed_at`. Synthetic tombstones derived at diagnosis time from absence in a listing (`cluster:missing`) never qualify.
   - **RECOVERED** — a status observation of the exact Pod (same uid) taken at time S with S ≥ onset + grace (§7 grace), showing condition `Ready=True` with `lastTransitionTime` R where R ≤ onset. Because `lastTransitionTime` changes on every Ready transition, the Pod was continuously Ready on [R, S], which covers [onset, onset + grace]. E = R.
5. **No overlap.** Every Finding time is strictly before E.

Neutral cases, which never trigger the rule: no journal or status record, `NO_DATA`/`UNKNOWN`, a status observation taken before onset + grace, `Ready` unknown or false, a uid mismatch, an untimed Finding, or any Finding at or after E. Missing evidence never ends an episode.

Audit fields: `rule_id=m16.ended-manifestation-episode`, `rule_version=v1`, `consequence=ROOT_INELIGIBILITY`, `mechanism=EPISODE_TIMING`, `targets=(pod canonical,)`, `evidence_ids` = manifestation evidence plus the tombstone/status evidence id, `time_basis` with onset, boundary, E and S, and the five preconditions.

### Scope limits and residual risk

- The rule only removes manifestation-only alternatives. It never makes any hypothesis `SUPPORTED`, never adds dominance, and never selects an actor. A `RESOLVED` outcome still requires the unchanged §4/resolution conditions for the remaining hypotheses.
- Residual risk: a Pod whose failures ended before onset could still have caused latent damage that surfaces later. A hypothesis carrying only manifestation evidence has no positive causal premise for that mechanism, so this is accepted and must be monitored by G16.9.
- Not scenario-keyed. No names, namespaces, labels, harness knowledge or ground truth may enter the rule.
- Harness cleanup that removes stale events is not G16.7 evidence (§13).

### Observation prerequisite

The object journal deliberately hashes desired state only (status is excluded), so the Pod status at diagnosis time is not retained as a version. RECOVERED therefore requires the observation layer to expose a typed, read-only status observation per Pod (observed-at, uid, Ready condition, evidence id). This is observation-plane data, not a change to the journal's version semantics.

## 18. Amendment A2 — resource-pressure mechanism mismatch (`m16.v1-a2`)

Status: **FROZEN** (owner-approved direction 2026-09-24). This amendment adds one mechanism-mismatch rule under §6 and §8. It promotes `OBSERVED_NORMAL_MECHANISM_MISMATCH` from a proposed name (§11) to an authorized code for this rule only. Sections 1–17 remain binding.

### Which hypothesis requires resource pressure

`RESOURCE_PRESSURE` is a manifestation kind in the hypothesis builder, never an initiating premise. A hypothesis *requires* the resource mechanism only when its initiating premise is a resource-limit reduction:

- R1. The causal actor is a workload (`Deployment`, `StatefulSet`, `DaemonSet`), and every initiating Finding of the hypothesis is a `SPEC_CHANGE` on that actor.
- R2. The complete diff between the two journal versions referenced by each such Finding's evidence ids (recomputed from the versions, not from the truncated `changed_paths`) touches only `.spec.template.spec.containers[*].resources.*` (restart annotations excepted).
- R3. At least one container `limits.memory` or `limits.cpu` value decreased or was newly set. The tested mechanism set M is the resources that were lowered or newly limited. Changes that only raise limits or change requests do not qualify, and neither does a change that also touches images, env, probes, commands or any other field.

Any other hypothesis is outside this rule. Its resource observations remain evidence only.

### Metrics, binding and thresholds

- Memory: `max by (container) (container_memory_working_set_bytes) / kube_pod_container_resource_limits{resource="memory"}` (cAdvisor + kube-state-metrics), exactly the existing M18A `prometheus.resource` template. Pressure threshold 0.9 of the limit.
- CPU: `rate(container_cpu_cfs_throttled_periods_total) / rate(container_cpu_cfs_periods_total)`, the existing template. Pressure threshold 0.25.
- The thresholds are the existing `PRESSURE_THRESHOLD` values; this amendment does not change them. `OBSERVED_NORMAL` is the existing M18A classification: usable samples, peak below threshold, at least two samples, and both window edges covered within one 15-second step.
- Binding: the exact Pods of the actor that run the changed template. That means Pods owned by the ReplicaSet whose pod-template matches the post-change template, per object history, and that existed during the coverage window. The exact container names in the diff are also required. A Pod set that cannot be determined positively makes the rule inapplicable.

### Coverage window

`[max(T_change, onset − 5 min), onset + grace]`, where `T_change` is the Finding's observed change time and grace is the §7 grace. Each read stays within the one-hour bounded-query contract. Every bound Pod must be covered for every resource in M.

### When `OBSERVED_NORMAL` is counter-evidence

It is counter-evidence only when it is produced for an exact bound Pod and container, for a resource in M, over an interval that covers the coverage window, with its query descriptor and source observation ids retained. `NO_DATA`, `UNKNOWN`, a partial window or a proxy metric are neutral.

### Rule `m16.resource-pressure.v1`

Reason code `OBSERVED_NORMAL_MECHANISM_MISMATCH`; consequence `CONTRADICTION` (the required mechanism premise is positively contradicted). It applies only when all of these hold:

1. R1–R3 identify the required mechanism set M.
2. The bound Pod set is non-empty and positively determined.
3. Every (Pod, container, resource ∈ M) has `OBSERVED_NORMAL` over the full coverage window.
4. No positive pressure evidence exists anywhere in the bound set: no `RESOURCE_PRESSURE` Finding, no `OOMKilled` container termination and no `Evicted` event in the window.
5. The hypothesis has no other initiating premise (already implied by R1).

If any precondition fails or is unknown, the rule does not apply and the hypothesis keeps its prior state. The audit records mechanism `RESOURCE_PRESSURE`, targets (actor + bound Pods), the per-Pod time basis, coverage (`n/n pods × resources observed normal`), the query descriptor ids as `observation_ids`, and every precondition result.

### Implementation clarifications (2026-09-24, recorded with the v1 implementation)

Both clarifications only narrow where the rule applies:

- **Per-Pod coverage start.** A bound Pod created inside the coverage window cannot have samples from before it existed. Its coverage starts at `max(window start, metadata.creationTimestamp)` and must still reach `onset + grace`. A Pod whose creation time is unknown makes the rule inapplicable.
- **Binding in v1.** Only `Deployment` actors are bound: Pods are owned by a ReplicaSet whose template containers equal the post-change containers. `StatefulSet`/`DaemonSet` Pods are not positively bindable in v1, so the rule is inapplicable for them.

### Scope limits

- Normal resources contradict only the resource-limit mechanism. They say nothing about any other hypothesis, actor or mechanism (§6).
- The rule never supports another hypothesis and never selects an actor. `RESOLVED` still requires the unchanged resolution rules.
- Live demonstration requires the observation plane to scrape cAdvisor and kube-state-metrics. Until then every live resource read is `NO_DATA` and the rule is inert (§10).
