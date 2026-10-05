# M21 Causal Semantics Contract — actor-specific claims

Contract version: `m21.v3`; engine `2.1.0`; D1 remains `m21.support.change-onset-path.v2`.
Authorized replacement of the conflicting frozen v1 clauses, 2026-09-28.
The v2 clauses below govern current code. The original v1 text is retained below
as historical rationale; its default competition, D1 audit-only, dominance and
supported-leader provisions are superseded, not additional requirements.

## v3 explanation and decision closure (2026-09-29)

Actor-scoped formation and positive admission stay `claim_version=m21.v2`.
The decision trace is `m21.v3`; the additions below supersede v2's blanket
D1-only downgrade only when the named stronger rule fires. D1 remains possible
initiation, never observed execution.

`CausalExplanation` identifies source claim, exact explained claim/observations,
actors/instances, episode, directed mechanism, decisive raw observation IDs,
coverage, rule version, consequence and remaining uncertainty. It does not
transfer evidence ownership. A structural mechanism bridge alone cannot produce
an explanation or strong support.

- `m21.explanation.runtime-return.v1` records a paired non-success return with
  positively verified endpoints, exact Pod UID, matching claim episode and
  target observation provenance. It explains that return, not every local fact
  or the remote failure origin. Existing m16 propagated-effect eliminations
  remain distinct and auditable.
- `m21.explanation.observed-quota-rejection.v1` consumes a raw rejection event
  explicitly naming the quota and namespace and the rejected subject/UID. Its
  source claim must be admitted; the raw `Warning/FailedCreate` establishes
  its observed role even when incident initiation is undetermined. Later
  rejections in the same episode can explain later effects without supplying D1
  support. An initiating-timing contradiction does not negate a recorded later role;
  other positive source contradictions block explanation authority. Only
  a target whose **all** local observations are these rejection events is explained as a whole. Any other
  local change, including ambiguous-time changes, remains a rival. The shared
  event is one observation, not independent corroboration.
- `m21.explanation.controller-spawn.v1` (2026-09-30) explains an experiment instance by the
  schedule instance whose controller `Spawned` record names it, keyed by UID on both sides
  (an owner inferred from names has no UID and explains nothing). The schedule claim must be
  admitted, UID-bound and **supported**: an unresolved schedule never retires a supported
  experiment. The experiment leaves competition only when **all** of its local facts are
  that execution; any other local fact keeps it as a rival (`EXPLAINS_OBSERVATION`). The
  claim is audited, not deleted: its targets and interval stay for later execution rules.
  It states that executions of one recurring fault are not independent root causes; it does
  not state that the fault initiated the incident, grants no strong authority, and **never
  answers a frontier question**.
- Explanation cycles remain observation records with `EXPLANATION_CYCLE`;
  they cannot retire claims, answer frontier questions or assert independent
  mechanisms. Explanation never supplies its own source support.

`m21.support.observed-quota-rejection.v1` adds `OBSERVED_MECHANISM_CAUSE` only
when a D1-supported quota claim has a raw, explicitly named rejection at its
origin time and the actual rejected subject is an incident symptom. It does
not promote quota exhaustion, selectors, generic quota→service paths, or a
structural config bridge. Missing UID is not invented. Its supported scope is
that observed admission rejection, not why all quota demand arose or service
recovery.

`m21.support.observed-fault-execution.v1` (2026-09-30, owner-approved carrier: Schedule
instance) adds `OBSERVED_MECHANISM_CAUSE` from a chaos **execution witness** plus an
**incident-effect relation**. The witness is produced at the exact experiment instance:
its controller records give the target pod, `Applied` time and an observed `Recovered` time
(an interval whose end is unobserved proves nothing). The effect relation holds for a target
when the exact Pod claim of that target, in the same episode, is a declared incident symptom
of the holder, has a failure observation inside `[Applied, Recovered]` and **none before
`Applied`**. The execution interval must also be connected to the incident's onset
(amendment 2026-09-30, owner-approved): it must not end more than **5 minutes before** the onset and
must not begin after it (one second of event-timestamp resolution tolerated). An experiment that ended
long before the incident is not its execution, whatever effects it once had. `Spawned`, `Applied` or a name match alone confer nothing. The witness is carried
to the parent **Schedule instance** only through the exact `controller-spawn` records (UID on
both sides, same as the explanation rule); a Schedule carries the witnesses of its own
spawned experiments and no others. An experiment with no admitted parent Schedule claim is
its own holder under the same conditions. The holder must already be D1-supported, and the
timing contract's gates apply unchanged (`execution` is the fired state of this rule; a
sensitive one withholds strong authority). Scope: only the observed effect at that target.
The witness covers a declared symptom only where the target itself is that symptom, so a
service-level symptom stays uncovered and `RESOLVED` is not reachable without a verified
path to it. The fault action (`spec.action`) is not observed, so the record states the
chaos kind, never a specific action. Recovery stays `NOT_ASSESSED`.

**Service-level effect relation (specified 2026-09-30; not implemented).** A second way for the
incident-effect relation to hold, for targets that are not themselves an incident symptom: a
direct paired non-success propagation edge whose source service is the target's service and
whose affected service is a declared incident symptom service, with a `VERIFIED` binding at
both ends, first observed **at or after `Applied`** and continuing no later than `Recovered`.
An edge already present before `Applied` proves nothing about the fault. A call-graph path
alone is never an effect: on the seen scenarios 74% of fault targets have one regardless of
what they did. Multi-hop chains and service names derived from pod names are not defined
here. **Implementation is deferred** until the own-testbed benchmark can measure this
relation against world-level ground truth: on the seen scenarios it holds for 1 target in 1
scenario, and fixing its parameters on that one case would tune the rule to it.

`MECHANISM_VERIFIED_CAUSE` / legacy `RESOLVED` requires one eligible supported
claim, execution witnesses covering **every declared incident symptom**, no
unresolved admitted claim, and no unanswered material frontier. The decision
basis is `OBSERVED_MECHANISM_DISAMBIGUATED_V1`. D1-only unique support remains
`SUPPORTED_CAUSE` / `AMBIGUOUS`. Multiple distinct actors with disjoint direct
execution observations may be `MULTIPLE_OBSERVED_CAUSES`; they remain ambiguous
for the legacy single-root API. Other admitted competition is `COMPETING_CAUSES`
and does not assert causal independence merely from the presence of two claims.

`FrontierAnswer` asks which observed upstream role affects the bound claims.
A positive explanation may answer that scoped question and transfer it to its
source claim (`ANSWERED_ROLE_TRANSFERRED`). Every bound claim question must
be covered; partial answers retain their witnesses and remain open. The source still requires
adjudication; this is not a proof of universal non-causality. A relevant positive
contradiction withdraws the answer on recomputation. Unrelated observations do
not. Quota declarations with pod-admission limits can create an investigation
question for affected workloads; namespace membership alone still cannot admit
or support a claim.

Frontier causal answers are separate from investigation progress:
`UNEXPLORED`, `INVESTIGATED_INCONCLUSIVE`, `ANSWERED`, `BLOCKED_ACCESS` and
`BLOCKED_BUDGET`. NO_DATA, an empty read, PROMOTED and exhaustion never answer
a question. Answered questions leave the material query queue; open questions
remain visible with their terminal reason. Existing physical-read identity and
attempt budgets prevent repeated identical unsuccessful reads.

Decision-bearing explanations, execution witnesses and frontier answers enter
the canonical digest. Terminal access/budget bookkeeping does not. Action audits
record before/after causal digests, so scoped support/answer transitions count
even when the legacy label stays ambiguous. Engine-version replay remains
strict: a 2.0.0 tape is not silently interpreted by 2.1.0. Legacy documents and
v2 digest projections remain readable as recorded. Reports are version 2.3.

Product proof T4 and T5/T6 ablations explicitly identify
`m21.unique-possible-cause.v1` for v2/v3 records, rather than presenting that
weaker success as the old definitive-resolution proof. Artifacts separately
report observed mechanism, positive rival elimination, strong disambiguation
and recovery. Recovery stays `NOT_ASSESSED`.

**Leader selection by epistemic tier (APPROVED and implemented 2026-10-01; roadmap C12; `leader_by_tier` in `packages/rca/presentation.py`).** Today, unless the
state is `RESOLVED` or exactly one claim is supported, the reported `root_cause` is the first selectable
hypothesis in ranking order (score, then canonical name), so with two or more supported claims an unsupported
candidate with an equal or higher score can be reported ahead of them. Proposed rule for every non-`RESOLVED`
selection: (1) take the highest epistemic tier that contains a selectable, not eliminated hypothesis (strong,
i.e. mechanism-verified, then supported, then the rest); (2) within that tier, rank by score; (3) break a
remaining tie by canonical name, which keeps replay deterministic and is never causal evidence. `RESOLVED`
selection, the ranking itself, admission and every support rule are unchanged; the operator's projection
(`packages/rca/presentation.py`) already applies the same tiers. To be decided on a shadow measurement over the
35 ITBench scenarios and the clean testbed incidents: changes in `root_cause`, diagnosis status, epistemic digest
and benchmark correctness, whether any supported or strong outcome is lost, and whether a corrected case is
merely replaced by another wrong actor.

*Shadow measurement (2026-10-01).* ITBench, 35 scenarios: no change at all (same `root_cause`, status, strong
witnesses, epistemic digest and correctness; 18 of 31 scoreable correct either way). Testbed, 81 incidents
(the four databases contaminated by the manual `diag-stress` experiment excluded whole): one `root_cause`
changed, from an unsupported `order-service` pod to the supported `StressChaos/pod-stress-1` that the run
injected, which is the true cause; its status (`COMPETING_CAUSES`), claim level and strong witnesses unchanged,
its digest changed accordingly; no supported or strong outcome was lost and no case moved to another wrong actor.
Approved by the owner on this measurement; replaying the corrected incident with the implemented rule gives the injected experiment, shown as the single supported cause.

## Preserved v2 decision contract (v3 exceptions above)

The stages are observation → presentation episode → actor/instance/incident
claim → positive admission → support/contradiction → scoped diagnosis.
`Hypothesis` is the claim representation; no parallel hypothesis hierarchy is
introduced. Every actor in a presentation component is retained, including two
independent initiators and manifestations. Exact UID and incident-onset partitions
are separate claims. Unknown UID observations never donate evidence to a known
UID. A known current UID mismatch or a different incident onset removes the
claim's current incident links. The former pre-group fault-instance collapse is
not used by the engine: choosing one scheduled execution could lose a cause.

Claim identity incorporates actor, UID when known, mechanism class, incident
onset and reached symptom scope; source IDs distinguish revision-local evidence.
Presentation group membership and rank never confer causal authority. Scores
order presentation and investigation only. Group findings remain available for
inspection; support, contradiction, roles and episode eligibility use actor-local
premises. There is no transitive group attribution.

### Admission and preserved uncertainty

`ADMITTED` means an actor-local observation has a validated directed mechanism
chain to a real alert-derived incident entity, or is itself directly alerting.
This is weaker than support: a related failure with no initiating evidence
remains a rival. Paths must start at the actor, be contiguous, use modeled causal
relations, and end at the declared incident symptom. Actor→member paths are stored
separately and never satisfy this predicate. Opaque `PATH`/`linked_symptoms`
strings, namespace/node co-location, rank and another actor's findings are not
admission evidence. A verified observed non-success propagation chain is also
usable, only at positively verified Kubernetes binding levels; Pod edges require
the current exact UID. This establishes propagation, not initiation.

`OBSERVED_CONTEXT` is retained and can be promoted by new evidence. It is neither
contradicted nor proven noncausal, and generates no queue to prove every observed
object innocent. Existing positive temporal, runtime propagated-effect, ended
instance and observed-normal mechanism rules remain applicable. Topic A's
covered-no-path requirements still govern a *positive noncausality assertion*;
they are not prerequisites to keep an unlinked observation as context.

Preserved: **Missing evidence is not contradiction or proof of non-causality.**
Replaced: **Every observed object remains a root competitor until eliminated.**
The resolver compares admitted claims, retaining every unresolved real rival.
Finding-kind set inclusion cannot explain another independent cause: dominance
is disabled until a positive causal explanation rule is supplied. Multiple
independent supported causes remain explicitly ambiguous.

### D1 witness and authority boundary

D1 v2 requires an actor/instance/episode-owned initiating change, a known causal
onset, an observed timestamp, source evidence IDs and an admitted incident link.
It emits `CausalWitness` records with the origin Finding, direct ownership,
mechanism, instance, actual symptom, directed chain, onset, source and available
relation observation IDs, coverage, missing execution proof, and rule version.
Quota rejection has one explicit attribution rule: the quota normalizer's
positive rejection evidence and exact rejected workload targets authorize
`quota_blocks`; generic `Finding.related` does not.

The level is **POSSIBLE_INITIATING_CAUSE**, not proven mechanism execution.
Runtime failure origin alone does not fire this initiating support rule. It
cannot eliminate an upstream configuration or unobserved dependency.
A unique supported eligible claim with no admitted rival yields
`diagnosis_status=SUPPORTED_CAUSE`. Competing admitted claims yield
`COMPETING_CAUSES`; absence of support yields `INSUFFICIENT_EVIDENCE`.
The legacy `resolution` remains `AMBIGUOUS` for a D1-only supported diagnosis:
D1 v2 does not grant legacy `RESOLVED` authority. This applies equally with or
without unrelated context. No conclusive initiating-proof rule is introduced.

### Material frontier and planner

The existing `StructuralAlternative` representation is extended with affected
entities and explicit claim bindings. Frontier construction runs for full
snapshots as well as bounded initial views. A modeled upstream dependency,
configuration, autoscaler, policy or fault affecting a node on an admitted
actor→incident chain is material to that claim's initiating-cause boundary.
Unrelated global coverage limitations do not automatically block the diagnosis.
Material gaps state the mechanism and affected claim, and reuse existing
bounded authorized queries, information gaps and read-cost policy.

`NO_DATA`, provider failure, completed reads, promotion and exhausted budget
are not causal closure. In particular, `QUERIED_NO_CAUSAL_FINDING` remains an
observational lifecycle status, never a proof of absence. Material mechanisms
remain visible after such reads. There is deliberately no generic frontier
closure rule: positive mechanism-specific closure must be implemented before a
future stronger initiating-proof rule can use it. Unmodeled dependencies are not
invented as object competitors; the possible-cause scope remains explicit.

The planner uses the same admitted IDs and material frontier bindings: complete
an initiating witness, distinguish real rivals, examine a material upstream
mechanism, or test a propagated manifestation. Context IDs receive no causal
priority, and frontier bindings outrank incidental structural observations.
Priority remains search guidance, never resolver authority.

### Product, inventory and replay migration

Engine `2.0.0`, `claim_version=m21.v2`, `ResolutionTrace.semantics_version=m21.v2`
and `Diagnosis.decision_semantics=m21.v2` make the transition explicit.
`diagnosis_status`/`claim_level`, `investigation_status`, and
`incident_recovery=NOT_ASSESSED` are separate axes. Console DTO, web workspace,
standalone HTML and report snapshots expose the scope. Existing incident alert
recovery and lifecycle transitions are unchanged. `root_cause` remains a legacy
selected-actor field, not new proof of incident recovery; consumers must read
its diagnosis status and scope. Legacy RESOLVED consumers receive no new
RESOLVED values from D1 v2.

All claims, audits, admissions, support witnesses and frontier bindings are
retained without an eight-record decision/measurement cap. Decision-bearing
witnesses, admission and material frontier enter the epistemic digest in sorted
canonical form. Scores do not. Legacy documents remain readable with `legacy`
semantics; unversioned claims are not auto-admitted. Existing replay engine
version checks reject old runs explicitly as unsupported, rather than applying
new meaning to old decisions. Replay of newly recorded runs uses recorded reads.
Recalculation of old evidence requires a new engine-versioned run.

## Historical v1 text (superseded where stated above)

Historical contract version: `m21.v1`
Status: **FROZEN** (owner, 2026-09-27, after amendments 1–2; amendments 3–5 recorded 2026-09-28, §9). M21 implementation starts only after the M20 live-parity tasks, in the owner-frozen order.
Baseline: `main` @ `d0caf5d0e3512930c1a8d4e0803e777a64fb2f06` (RCA code identical to the M20.1 audit)
Scope: this contract defines the evidence that authorizes the RCA engine to treat a hypothesis as root-capable, root-supported or root-ineligible. It covers four topics: A unrelated initiated changes, B telemetry and control-plane actors, C asynchronous propagation, and D positive root support. It changes no code, test or threshold.

`m16.v1` (including amendments A1 `m16.ended-manifestation-episode.v1` and A2 `m16.resource-pressure.v1`) and `m18a.v1` remain binding. Where this contract is silent, they govern. This contract adds nothing to them and relaxes none of their clauses.

## 1. The question this contract answers

> By which positive or negative causal evidence may we treat a hypothesis as **root-capable**, **root-supported**, or **root-ineligible**?

| Term | Meaning | Existing mechanism |
|---|---|---|
| root-capable | The hypothesis may compete for root cause. This is the default for every generated hypothesis. | `RootCauseEligibilityState.ELIGIBLE` / `UNDETERMINED` |
| root-supported | A named deterministic rule has fired on positive evidence that the actor initiated the incident. | Today only the implicit `SUPPORTED` predicate (§5.4) |
| root-ineligible | A named deterministic rule excludes the actor from root competition for this incident, **without** denying that its observations happened. | `ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT`, A1 |
| contradicted | A required premise of the hypothesis is positively incompatible with evidence. | Temporal contradiction, A2 |

The resolver keeps its current contract: `RESOLVED` requires exactly one root-eligible `SUPPORTED` leader (or a unique dominator) and **zero** root-eligible `UNRESOLVED` alternatives (`UNRESOLVED_CAUSAL_ALTERNATIVE`: "missing proof does not exclude"). This contract does not change that rule. It only defines new, narrow, positively evidenced ways for a hypothesis to leave competition or to become supported.

## 2. Measured problem (M20.1)

On the ITBench dev split (10 otel-demo incidents), all 10 final diagnoses are `AMBIGUOUS`, and 54 root-eligible `UNRESOLVED` leaders block resolution. The root ranks first in 9/10. The M20.1 audit (`.local/m20-audit/`) classifies the blockers as follows:

| Class | Blockers / incidents | Current state | Topic |
|---|---|---|---|
| C1 unlinked initiated change | 25 / 10 | `SOURCE_CAPABLE`, `ELIGIBLE`, `NO_CAUSAL_SYMPTOM_LINK` | A (and B) |
| C2 telemetry-infrastructure manifestation (otel-collector Pods) | 10 / 10 | `UNKNOWN`, `UNDETERMINED`, 0 traced incoming edges | B |
| C5 traced service, manifestation or post-onset change, no propagation evidence | 10 / 7 | `UNKNOWN`, `UNDETERMINED`, 0 verified incoming propagation | D (and C) |
| C4 async or untraced actor (kafka, fraud-detection, valkey-cart, image-provider) | 7 / 3 | `UNKNOWN`, `UNDETERMINED` | C |
| C3 non-service manifestation (flagd-config ConfigMap) | 2 / 2 | `UNKNOWN`, `UNDETERMINED` | D |

The C1 population is informative. It consists of seven `data-recorders` Namespace creations and four `agent` Namespace creations; recorder ConfigMaps and Jobs (5 + 2); two `kube-root-ca.crt` ConfigMaps (auto-created in every new namespace); and five `TRAFFIC_INCREASE` on `prometheus-kube-state-metrics`. All of them are benchmark-harness or observation-plane objects. **No rule may key on these names** (§3 I3). The contract must instead say what positive evidence would show that such a change cannot reach the symptom. Where that evidence is missing, the change stays in competition.

Measured facts about the evidence currently available:
- Offline ITBench feeds traces (20k–292k spans per incident) and traffic into base RCA. Live base RCA does not (M20.4/M20.5).
- The runtime graph already classifies a `PRODUCER → CONSUMER` parent/child pair (`RuntimeEdgeEvidence.PAIRED_PRODUCER_CONSUMER`). `derive_runtime_propagation` counts it as `async_pairs_uninterpreted` and discards it. The dev split has `producer_consumer_pairs = 0` in all 10 incidents.
- `TraceSpanObservation` keeps `span_kind` and a bounded attribute set (including `messaging.system` and `messaging.destination.name`). It has **no span links**.
- The deterministic investigation eliminates 0 alternatives in all 10 incidents.

This contract does not predict how many blockers any rule will remove. The effect is measured once, at the M21 gate. A rule that turns out inert under honest coverage is an acceptable result. A rule relaxed until it fires is not.

### 2.1 Full census (amendment 3)

The 54 above is a measurement bug, not the product's state. The M20.1 audit counted the persisted `HypothesisResolutionAudit` list, which `_audit_items` truncates to `_MAX_TRACE_ITEMS = 8`, while the resolver decides over every hypothesis. No benchmark or forensic measurement may again count hypotheses or blockers from `resolution_trace.hypothesis_audits`; it must use `considered_hypotheses`, `unresolved_hypotheses`, the full engine structures, or a dedicated full audit output.

Full census, ITBench dev split (`.local/m20-audit/m20_6/`):

| Class (measurement label) | Unresolved blockers |
|---|---|
| C1 initiated change | 45 |
| C2 telemetry, control-plane and infrastructure manifestation | 43 |
| C5 traced application service | 10 |
| C4 async or untraced actor | 9 |
| C3 non-service object | 4 |
| **Total** | **111** |

A separate problem, not inside the 111 because it is `SUPPORTED`/plausible: an **off-symptom plausible initiated rival** (the "R cohort") in 9/10 incidents. Its support comes from recorder objects in a namespace with no incident symptom. The labels are measurement labels only and are never production predicates.

## 3. Global invariants (all topics)

- **I1 — Missing is not exclusion.** `NO OBSERVED LINK ≠ PROVEN NO LINK`. None of these may exclude, contradict or support anything: absence of a path, `NO_DATA`, `UNKNOWN`, provider `ERROR`, a partial window, an uninstrumented actor, or an unqueried channel.
- **I2 — Asymmetric evidence strength.** Weak evidence may *keep* an alternative in competition. Only strong, complete, positively covered evidence may *remove* one. For example, an aggregate destination-level async relation (MSG-3, §5.3) counts as an open channel for A, but never establishes async propagated-effect ineligibility.
- **I3 — Truth-blind and name-blind.** No rule may depend on scenario IDs, ground truth, or specific object, namespace, image or label *values* chosen to match a benchmark. Actor classification comes only from typed evidence or from operator-declared, versioned configuration (§5.2.1). Any such configuration is part of the RCA config digest and is identical across every incident it is applied to.
- **I4 — Three-valued rule outcomes, no scores.** Every rule evaluates to `FIRED`, `NOT_FIRED` or `INAPPLICABLE`, and records every precondition result. No rule produces a weight, probability or support score, and ranking never feeds a rule.
- **I5 — Replayable inputs only.** Every rule reads only manifest members and provider-tape results of the run (M19). The M20.1b base replay must reproduce every new consequence digest. Coverage is computed from the same persisted inputs, never from a live re-query.
- **I6 — Authority boundary unchanged.** Only deterministic RCA consequence code may change eligibility, support or epistemic state. The planner, investigation tools, normalizers and any LLM have no authority (m16 §1).
- **I7 — Frozen rules untouched.** A1, A2, temporal contradiction and `ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT` keep their IDs, versions and meanings.
- **I8 — Audit.** Every consequence carries `rule_id`, `rule_version`, `consequence`, exact `evidence_ids` and source `observation_ids`, `targets`, `time_basis`, a `coverage` record (§4.3), and per-precondition results (m16 §11). Structured fields are authoritative; any explanatory text is secondary.
- **I9 — Exclusion is incident-scoped.** Root-ineligibility says only that the actor did not initiate *this* incident under the named rule. It never denies the observed change or failure.
- **I10 — Domain alone never excludes.** Domain classification (§5.2) may alter which channels are applicable or which coverage obligations exist. Domain classification alone MUST NEVER produce `ROOT_INELIGIBILITY`, `CONTRADICTED`, or an elimination.
- **I11 — No vacuous closure.** A causal-closure exclusion over an empty set of applicable channels is forbidden. Absence of an applicable channel set never proves non-causality (§4.2a).
- **I12 — Applicability is evidence-independent.** Channel applicability may depend only on deterministic, pre-decision structural or declarative facts:
  - closure object kinds;
  - placement and lifecycle;
  - typed candidate and symptom kind (for example `TRAFFIC_INCREASE`);
  - operator-declared actor domain (§5.2.1);
  - declared alert or provenance class.

  Applicability MUST NOT depend on whether the channel produced normal, abnormal, supporting, contradictory, missing or `NO_DATA` observations in this incident. Observed channel evidence determines `PATH` / `NO_PATH_COVERED` / `UNCOVERED` / `UNKNOWN`. It never determines whether the channel exists.
- **I13 — Support is not elimination.** Support never excludes a rival and never carries resolution authority (§5.4).

## 4. Shared framework: influence channels and coverage

Topics A and B (and the async part of C) share one idea. An actor can only have caused a symptom through some **influence channel**. The actor can be excluded only when every channel that *applies* to its closure is **positively covered** and shows no path to the symptom. Channel applicability (§4.2a) is distinct from channel coverage (§4.3).

### 4.1 Channel catalog (`m21.channel-catalog.v1`, proposed)

| Channel | Meaning | APPLICABLE iff (evidence-independent structural/declarative, §4.2a) | Path evidence (keeps the actor alive) | Coverage evidence (needed to claim "no path") |
|---|---|---|---|---|
| K — Kubernetes reference | Owner/selector/routing/config/secret/volume/env/service-account/network-policy/quota/limit-range/PDB/HPA/scheduling references, including namespaceSelector and cross-namespace references | Always (every object has a reference surface) | Any reference chain from the change closure (§4.2) to a symptom-side entity | Object journal complete for every namespace and cluster-scoped kind in the window, **and** the relation extractor declares complete reference coverage for every kind in the closure (a versioned per-kind table). Amendment 5: the exact conditions are K1–K6 of `m21.k-reference-coverage.v1` (§4.5) |
| R — synchronous runtime | Traced CLIENT→SERVER calls | The closure contains a Pod instance that ran in the window, or a Service | Any traced call edge between closure services and symptom-side services, in either direction, in the window | Every Pod in the closure is instrumented (it emitted spans in the window) and trace reads for the window completed without error or truncation |
| M — asynchronous messaging | PRODUCER → destination → CONSUMER | The closure contains a Pod instance that ran in the window | Any message-level or destination-level relation (§5.3) between the closure and the symptom side | Messaging instrumentation present on the closure's producers and consumers, and complete trace reads |
| N — shared-node causal channel | CPU, memory, disk, network or PID contention on a shared node | The closure contains at least one Pod instance that was scheduled and running on a known node during the window (OD-A1) | A closure Pod and a symptom-side Pod shared a node in the window | All relevant placements known for the whole window **and** no closure Pod shares a node with a symptom-side Pod. A shared node is `UNCOVERED` in v1 (OD-A1) |
| C — cluster control plane | Admission webhooks, CRDs/operators, RBAC, PriorityClass/preemption, DNS, CNI, API-server load | The closure contains a cluster-scoped or control-plane kind. Amendment 5 (§4.5.4) adds: a closure Pod that can preempt a symptom-side Pod; and an access by a recognized control-plane identity to a closure object | The closure contains a cluster-scoped or control-plane kind, or an object that registers a webhook or controller | v1: **never covered.** Any closure touching these kinds is `INAPPLICABLE` for exclusion |
| O — observation path | The actor transports, stores or computes telemetry the symptom was measured from | Always for `TRAFFIC_INCREASE` (OD-A3) and for declared `TELEMETRY` actors (§5.2). Otherwise when the closure contains a Pod instance that ran in the window, or a Service | The alert or symptom signal's provenance passes through the actor | The alert's signal provenance is known and positively does not pass through the actor |

Channel O matters because a telemetry actor can create or hide a symptom without touching the application (§5).

### 4.2 Change closure

The **closure** of an initiated change on object X is X plus every object X owns, contains, creates or configures within the window, via channel K. Examples: the Namespace plus the objects inside it; the Job plus its Pods; the ConfigMap plus the Pods that mount it. Channel evaluation applies to the whole closure. If the closure cannot be computed positively (unknown kinds, CRDs, missing journal data), the rule is `INAPPLICABLE`.

### 4.2a Channel applicability

Channel applicability is distinct from channel coverage. Each catalog channel is first classified as `APPLICABLE` or `NOT_APPLICABLE` for the candidate's closure, using only the evidence-independent structural or declarative predicate in §4.1 (I12).
- A channel that cannot mediate the candidate's mechanism is `NOT_APPLICABLE`. It is not `UNKNOWN` and not `UNCOVERED`.
- `NOT_APPLICABLE` channels do not participate in causal closure.
- Applicability MUST NOT depend on observed CPU, memory, pressure, eviction, throttling, OOM or any other abnormal signal. For channel N it depends only on placement and lifecycle facts: a Pod instance was scheduled and running on a known node in the window.
- If a structural fact needed to decide applicability is missing (for example, whether a closure Pod ran in the window), the channel is treated as `APPLICABLE` with coverage `UNKNOWN`. Missing facts never make a channel inapplicable.

The applicability decision is recorded per channel in the coverage record, together with its structural basis.

### 4.3 Coverage record

Each channel evaluation produces a record containing:
- the channel;
- `applicability ∈ {APPLICABLE, NOT_APPLICABLE}` and its structural basis;
- for `APPLICABLE` channels only, `state ∈ {PATH, NO_PATH_COVERED, UNCOVERED, UNKNOWN}`;
- the window;
- the basis evidence and observation ids;
- the reason for any gap.

A causal-closure exclusion requires all of:
1. at least one applicable channel;
2. every applicable channel to be coverage-complete;
3. every applicable channel to prove `NO_PATH_COVERED`;
4. no applicable channel in state `PATH`, `UNKNOWN` or `UNCOVERED`.

Outcomes:

| Applicable channel states | Rule outcome |
|---|---|
| No applicable channel (empty set) | `INAPPLICABLE` (I11) |
| Any `PATH` | `NOT_FIRED` |
| Otherwise, any `UNCOVERED` or `UNKNOWN` | `INAPPLICABLE`; the alternative stays in competition |
| All `NO_PATH_COVERED` (non-empty set) | `FIRED` |

### 4.4 Temporal coverage

The window is `[T_change − L, onset + grace]`:
- `T_change` is the change's causal time (m16 §7);
- `L` is the look-back needed to establish the pre-change state (proposed: the journal's previous version of each closure object);
- `grace` is the frozen m16 grace.

Every channel's evidence must cover the entire window. A change whose time is interval-uncertain against the window is `INAPPLICABLE`.

### 4.5 Channel K reference coverage: `m21.k-reference-coverage.v1` (amendment 5)

This section defines when channel K may be `NO_PATH_COVERED`. K applicability is unchanged: K is always `APPLICABLE` (I12). The rule is **fail-closed by construction**. On the evidence available today, neither the product nor ITBench collects Kubernetes audit logs, so K6 cannot pass, and `NO_PATH_COVERED = 0` is the expected and accepted result. No completeness or reader-absence declaration is added to any benchmark to obtain yield (OD-K3, OD-K4).

#### 4.5.1 Preconditions

Every precondition is evaluated and recorded as `PASS`, `FAIL` or `UNKNOWN`, with its basis and evidence ids. The decision may short-circuit; the audit record never does (I4, I8).

- **K1 — Continuous journal coverage, positively proven.** The object journal must carry an explicit, persisted coverage fact for the window:
  - `coverage_start ≤ window start` and `coverage_end ≥ window end`;
  - `covered_namespaces` ⊇ every closure namespace and every symptom-side namespace, plus cluster scope;
  - `covered_kinds` ⊇ the kinds of the frozen profile (§4.5.2) and the RBAC kinds;
  - `continuous = true` and `gap_count = 0`;
  - `source` and `source_version`.

  Watch or list continuity, or equivalent persisted provenance, is required. A point-in-time LIST, a set of completed listings, or the rows happening to be present in a dataset is not such a fact. When the fact is missing, K1 is `UNKNOWN`.
- **K2 — Supported-kind profile.** Every closure kind must be `COMPLETE` in the frozen per-kind profile (§4.5.2). Otherwise K2 is `FAIL`.
- **K3 — Namespace separation (v1, OD-K1).** No closure member shares a namespace with the symptom side (the full OD-A4 set). Otherwise K3 is `FAIL`, with reason `SAME_NAMESPACE_REFERENCE_SURFACE_INCOMPLETE`. This is a conservative v1 boundary. It is not a claim that a shared namespace is a causal path.
- **K4 — Complete traversal.** The causal path search runs over a finite persisted graph with a visited set and **no semantic hop cap** (no `max_depth` of 4, 6 or any other policy value). The graph is the union of the edges derived from **all relevant versions in the causal window**, not only the latest versions. A found path makes the state `PATH`. A depth-limited or latest-only search may prove `PATH` but never its absence.
- **K5 — No known unsupported reference surface.** None of the following may be present in the window. Each occurrence found is `FAIL`, with its reason:
  - a NetworkPolicy peer using `namespaceSelector`;
  - a Service of type `ExternalName`;
  - a pod (anti-)affinity term with `namespaces` or `namespaceSelector`;
  - a selector the extractor does not evaluate (`matchExpressions`, or chaos selector fields other than `namespaces`/`labelSelectors`);
  - a Pod with `ephemeralContainers`;
  - any structural or reference-bearing field outside the frozen profile, or an object or schema outside the profile's API-version range.

  Values inside free-form maps (ConfigMap `data`, labels, annotations) are not schema fields. Their influence through API readers is handled by K6.
- **K6 — API surface positively covered** (§4.5.3). A negative operator assertion ("there is no other reader") never satisfies K6.

#### 4.5.2 Per-kind profile

`m21.k-reference-coverage.vN` fixes a supported Kubernetes API version range. For each kind it lists:
- its reference surfaces, both inbound and outbound;
- the modelled surfaces;
- the explicitly uncovered surfaces;
- the tests.

A kind is `COMPLETE` only when every causally relevant reference surface of the frozen schema is classified, and every modelled surface has a test. Promoting a kind is a versioned profile change, approved by the owner. Writing this contract does not make any kind `COMPLETE`.

Initial profile (v1):

| Kind | Status | Modelled today (`topology.derive_edges`) |
|---|---|---|
| Namespace | INCOMPLETE | contains its objects (closure only) |
| ConfigMap | INCOMPLETE | consumers through Pod and workload-template volume, projected, envFrom and env valueFrom |
| Service | INCOMPLETE | selector → Pod (matchLabels only); `routes_to`; env-declared hosts (`calls`) |
| ServiceAccount | INCOMPLETE | none; Pod `spec.serviceAccountName` is not modelled |
| Job | INCOMPLETE | ownerReferences; template config references |
| Pod | INCOMPLETE | ownerReferences; config references (containers, initContainers); Service/NetworkPolicy selection (matchLabels) |

`Secret` stays outside v1 (OD-A2).

#### 4.5.3 API surface (K6)

An RBAC permission is **potential surface only**. It is never a `PATH`, and on its own it never proves absence.

**Direction.**
- An external identity reads a closure object. The closure may influence the reader, so the reader's workload joins the closure.
- A closure identity writes an external object. The closure may influence that object, so the target joins the closure.
- A closure identity reads an external object. This is an incoming dependency. On its own it is not a K path out of the candidate.

**Coverage.**
- A permission exists but the API audit evidence is incomplete: `UNCOVERED`.
- A permission exists, the audit evidence is complete (below), there is no actual relevant access, and no watch crosses the window: K6 may `PASS`.
- There is an actual access by an identity that cannot be bound exactly to a workload (User, Group, or an unbound ServiceAccount): `UNCOVERED`.

**Complete audit evidence** requires all of these:
- **Window coverage.** Audit coverage spans the window for **every serving API-server instance** (HA included).
- **Policy provenance.** The recorded audit policy positively covers the relevant API groups, resources, verbs and stages.
- **No lost evidence.** No dropped or truncated audit evidence. The source is `apiserver_audit_error_total` (or an equivalent loss counter) per instance. An unchanged counter (start = end) means zero new errors only when the continuity of the same API-server process and series is proven. A counter reset, process restart, scrape gap, missing replica or disappeared series makes the coverage `UNKNOWN`.
- **Crossing watches.** Every watch that was active at the window start is known. A watch opened before the window keeps receiving changes during it, so the long-running request lifecycle (for example the `ResponseStarted` stage) is modelled. If the pre-window active-watch state is unknown, K6 is `UNCOVERED`.

**Collection requests.** A LIST or WATCH without an object name, whether namespaced or cluster-wide, counts as potential access to every object of the requested kind within the request scope. A selector that the evaluator cannot interpret safely and completely is not used to narrow that scope. The scope is recorded from the audit event (`requestURI`, `verb`, `objectRef`, `user`).

**Kubelet attribution (exact).** An access by `system:node:N` is attributed to a Pod, and not counted as an independent external reader, only if all of these hold:
- the audit identity is `system:node:N`;
- an exact Pod instance is scheduled on N during the access;
- that Pod's spec positively references the object;
- the accessed resource and object match that reference.

If several Pods qualify, all of them are recorded, in deterministic order. If no exact Pod-and-reference binding exists, the access is `UNCOVERED`. There is no "the node read it, so it was probably for this Pod" inference.

**Control-plane identities.** An access by a recognized control-plane identity (controllers, garbage collector, namespace controller, …) is not counted a second time as a K6 external reader. It makes channel C `APPLICABLE`, and C is `UNCOVERED` in v1 (§4.5.4). Recognition comes only from a versioned identity-classification profile, never from an ad-hoc string list. An unrecognized `system:*` identity is not a control-plane identity by default.

**Impersonation.** The audit record keeps both the `authenticated_actor` and the `effective_actor` (the impersonated identity). Access is attributed to both for provenance. Closure expansion follows the effective access and does not blindly expand the closure twice for one access; the impersonator is never dropped from provenance.

**Fixed-point expansion.** Adding an actual reader, writer or target:
1. expands the closure;
2. is iterated to a fixed point with a visited set over the finite persisted graph, so termination is guaranteed;
3. recomputes K1–K6;
4. recomputes the applicability and coverage of R, M, N, C and O.

A new member that shares a namespace, shares a node or adds a runtime edge is evaluated naturally by the corresponding channel.

#### 4.5.4 Channel C additions (moved from K)

Preemption and control-plane identity access belong to channel C and are not modelled in K.
- **Preemption.** C is `APPLICABLE` when a closure Pod's resolved `spec.priority` is higher than that of a symptom-side Pod and its `spec.preemptionPolicy` is not `Never`.
  - When these structural facts are complete and such a Pod exists: C is `APPLICABLE` and `UNCOVERED`.
  - When a fact is required but missing (for example, `priorityClassName` is set but the resolved `priority` or `preemptionPolicy` is absent): C is `APPLICABLE` and `UNKNOWN`. It is never `NOT_APPLICABLE` (§4.2a).
- **Control-plane identity access** to a closure object (§4.5.3): C is `APPLICABLE` and `UNCOVERED` in v1.

#### 4.5.5 Final K state (deterministic precedence)

1. A positive modelled path exists → `PATH`.
2. Otherwise, any known incomplete or open influence surface → `UNCOVERED`.
3. Otherwise, any required completeness fact unavailable → `UNKNOWN`.
4. Otherwise, all of K1–K6 are positively satisfied and there is no path → `NO_PATH_COVERED`.

When several preconditions fail at once, all of them are recorded, and the precedence picks the state. For example, K2 `FAIL` together with K1 `UNKNOWN` gives `UNCOVERED`.

#### 4.5.6 Implementation and measurement gate

- **Audit only.** K audit implementation records K1–K6 and the §4.5.4 C additions. The records are outside the epistemic digest; no decision changes, and no engine bump.
- **Hard gates.**
  - 35/35 epistemic digests equal to `main`.
  - Replay passes.
  - No ground-truth hypothesis with K `NO_PATH_COVERED`.
- **Reported.**
  - the K state distribution;
  - the per-precondition distribution;
  - the change in C;
  - the §4.3 audit outcome distribution.

Negative controls (each mutation must be caught):
- a depth-5 path;
- a path that exists only in an earlier window version;
- a shared namespace;
- missing journal coverage;
- a K5 construct;
- an RBAC permission without audit evidence;
- a kubelet read without an exact Pod reference;
- a counter reset;
- an unknown pre-window watch;
- preemption → C;
- an unrecognized `system:*` identity not being treated as control plane;
- K applicability independent of evidence (I12).

#### 4.5.7 Owner decisions (2026-09-28)

- **OD-K1: APPROVED (a).** Namespace separation (K3).
- **OD-K2: APPROVED with direction clarification** (§4.5.3). A closure identity reading an external object is not, on its own, a K path out of the closure. API-server load is channel C, not K.
- **OD-K3: (b).** No frozen journal-completeness declaration for ITBench. Without independent provenance, K1 is `UNKNOWN`, and Topic A staying inert is accepted.
- **OD-K4: no reader-absence declaration in v1.** A future operator declaration may only be **positive**: it may add possible readers and surfaces, and it is never evidence of absence.
- **Preemption** moves from K to C (§4.5.4).

Amendment 5 is committed before the read-only census. **The K predicate is not changed after the measurement, whatever the result.** The owner then decides between Topic A authority and the next M21 slice.

## 5. Topics

Each topic uses the same headings: problem, current behavior, risk, proposed semantic, required evidence, coverage preconditions, missing-evidence behavior, false-resolved failure mode, versioned rule candidate, open owner decisions.

### 5.1 Topic A — Unrelated initiated changes

**PROBLEM.** A change that initiated nothing in the incident still competes for root cause, forever. It is `SOURCE_CAPABLE` with no derived path, so it is `UNRESOLVED` and blocks resolution. This is 25/54 dev blockers, found in all 10 incidents.

**CURRENT BEHAVIOR.**
- `hypotheses._make_hypothesis` sets `causal_explanation = UNLINKED` when `topology.causal_path` finds no path and no symptom is linked.
- `assess_hypothesis` adds `NO_CAUSAL_SYMPTOM_LINK` and leaves the state `UNRESOLVED`. The m16 §5 defect that made this `CONTRADICTED` is repaired.
- Root eligibility is `ELIGIBLE` (`SOURCE_CAPABLE_CAUSAL_ROLE`).
- The resolver returns `AMBIGUOUS` / `UNRESOLVED_CAUSAL_ALTERNATIVE`.

**RISK.** This topic carries the highest false-resolved risk in the contract. Topology derivation covers only the relations the extractor knows. An unlinked change may act through an unmodeled reference, an untraced call, a shared node, a control-plane side effect, or the observation path. Excluding it on the basis of no path found would re-introduce the m16 §5 defect in another form.

**PROPOSED SEMANTIC.** An initiated change is **root-ineligible for this incident** only when its full closure has a non-empty set of applicable channels (§4.2a) and every applicable channel evaluates to `NO_PATH_COVERED` over the full window (§4.4, §4.3). The consequence is `ROOT_INELIGIBILITY`, not `CONTRADICTED`: the change happened; it is just not this incident's initiator. The rule never supports another hypothesis and never selects an actor.

**AMENDMENT 3 — scope.** Topic A applies to every initiated-change hypothesis that carries an actor-aligned initiating change, **including `SUPPORTED` ones**, not only `UNRESOLVED` / `NO_CAUSAL_SYMPTOM_LINK` ones. Existing support does not prevent root-ineligibility when every applicable channel from the candidate's causal closure to the incident's symptom side is `NO_PATH_COVERED` under complete coverage. Any channel that is `PATH`, `UNKNOWN` or `UNCOVERED` keeps the candidate. The R cohort (§2.1) is the measured instance: a recorder ConfigMap with real support to recorder Pods but no covered influence on the symptom side. R is a measurement cohort name, never a separate rule; "all findings outside the symptom scope, no current path → eliminate" is rejected as too weak for I1/I2.

**REQUIRED EVIDENCE.**
- The exact change Finding with its causal time.
- The journal versions of every closure object.
- The symptom-side entity set (OD-A4): the alert's entities (the existing `symptom_entities`) plus their K-closure, plus **strict runtime neighbors**. A service is a strict runtime neighbor of a symptom-side service iff all of these hold:
  - a strict observed runtime edge connects them (`PAIRED_CLIENT_SERVER`, or message-level MSG-1/MSG-2);
  - the edge was observed inside the incident observation window;
  - the service identity is resolved (exactly verified binding);
  - the trace coverage requirement for that window is satisfied.

  A generic `CROSS_SERVICE_PARENT` edge, a fallback-only relation, a trace outside the incident window, or an unresolved identity does not add a neighbor.
- The per-channel coverage records with evidence ids.
- The trace read results used for channels R and M.
- Pod placement and lifecycle records for channel N applicability and coverage.

**COVERAGE PRECONDITIONS.**
1. The closure is computable and contains only kinds in the v1 closed kind set. The v1 set is Namespace, ConfigMap, Service, ServiceAccount, Job and Pod (OD-A2). `Secret` is excluded in v1.
2. At least one channel is applicable (I11).
3. Channel K: `NO_PATH_COVERED` under `m21.k-reference-coverage.v1` (§4.5, amendment 5).
4. Channels R and M, when applicable: every closure Pod is instrumented, and the trace reads for the window completed.
5. Channel N, when applicable: every relevant placement is known for the whole window, and no closure Pod shares a node with a symptom-side Pod. In v1, a shared node is `UNCOVERED`. An unknown or incomplete placement is `UNKNOWN` or `UNCOVERED` (OD-A1).
6. Channel C: when applicable it is always `UNCOVERED` in v1, so a closure containing a control-plane or cluster-scoped kind is `INAPPLICABLE`.
7. Channel O, when applicable: the alert's signal provenance is known and excludes the closure.
8. The change time is certain against the window.

**MISSING-EVIDENCE BEHAVIOR.** If any precondition is unknown or partial, the rule is `INAPPLICABLE`, the hypothesis keeps its current state and eligibility, and the diagnosis stays `AMBIGUOUS`. Two cases get specific treatment:
- A closure with running uninstrumented Pods cannot close channel R.
- A closure with a Pod on a node shared with the symptom side cannot close channel N in v1.

The five `TRAFFIC_INCREASE` blockers are observations, not operator changes. Under OD-A3 they are in scope, with channel O mandatory.

**FALSE-RESOLVED FAILURE MODE.** The dangerous case is a change that really initiated the incident, excluded because its channel was not modeled while the coverage records claimed completeness. Required negative controls for the M21 gate:
- (a) **Noisy neighbor.** A new-namespace Job whose Pod shares a node with the symptom workload, with node metrics unavailable: N is `APPLICABLE` and `UNCOVERED`, so the unrelated-change rule MUST NOT eliminate the Job.
- (b) A ConfigMap consumed through an unmodeled reference must be `INAPPLICABLE`, not excluded (channel K completeness).
- (c) A change reaching the symptom only through an uninstrumented service must be `INAPPLICABLE` (channel R).
- (d) A change to a webhook or CRD must be `INAPPLICABLE` (channel C).
- (e) **Vacuous truth.** A candidate with zero applicable channels MUST NOT be eliminated; the rule is `INAPPLICABLE`.
- (f) **Applicability is not evidence.** A candidate whose closure Pod ran on a node shared with the symptom side must keep N `APPLICABLE` even when no pressure, eviction or OOM signal exists.

**VERSIONED RULE CANDIDATE.** `m21.unrelated-change.v1` (proposed):
- reason code `NO_INFLUENCE_CHANNEL_UNDER_COVERAGE` (proposed new code);
- consequence `ROOT_INELIGIBILITY`;
- mechanism `INFLUENCE_CHANNEL`;
- targets: actor plus closure;
- audit: one coverage record per catalog channel, with its applicability.

**OWNER DECISIONS (2026-09-27).**
- **OD-A1 — AMENDED.** N is the shared-node causal channel. It is `APPLICABLE` iff the candidate closure contains at least one Pod instance that was scheduled and running on a known node during the relevant causal window. Applicability depends only on structural placement and lifecycle facts, never on observed CPU, memory, pressure, eviction, throttling, OOM or any other abnormal signal. Coverage:
  - all relevant placements known and no closure Pod shares a node with a symptom-side Pod → `NO_PATH_COVERED`;
  - all relevant placements known and at least one node shared → `UNCOVERED` in v1 (node-level normality needs its own future contract, like A2);
  - any required placement unknown or incomplete → `UNKNOWN` or `UNCOVERED`, so the rule is `INAPPLICABLE`.
- **OD-A2 — APPROVED WITH CHANGE.** The v1 closed kind set is Namespace, ConfigMap, Service, ServiceAccount, Job and Pod. `Secret` is excluded from v1 ("need before support"); it may be added later only as a versioned extension, once a need is shown. Everything else is `INAPPLICABLE`.
- **OD-A3 — APPROVED.** `TRAFFIC_INCREASE` is in scope. Its closure is the Service's callers and callees, and channel O is mandatory.
- **OD-A4 — APPROVED WITH BOUND.** The symptom side includes only strict runtime neighbors, as defined under REQUIRED EVIDENCE: strict edge, inside the window, identity resolved, coverage-qualified.

### 5.2 Topic B — Telemetry, control-plane and infrastructure actors

**PROBLEM.** otel-collector Pods with pre-onset or overlapping `FAILURE_EVENT`s remain `UNRESOLVED` leaders in all 10 dev incidents (10/54). The collector receives OTLP export rather than traced RPC, so it has 0 traced incoming edges and can never become a propagated effect. It can, however, be the real root of a telemetry-loss incident.

**CURRENT BEHAVIOR.**
- There is no actor role or domain concept.
- The collector hypothesis is manifestation-only with a `PATH` explanation (via topology), so it is `UNRESOLVED`, with causal role `UNKNOWN` and eligibility `UNDETERMINED`.
- A1 applies only when the episode has positively ended before onset.

**RISK.**
- A name blacklist (for example "never blame otel-collector") is scenario-fitted and wrong for telemetry-loss incidents.
- The opposite error is also possible: a collector outage can *fabricate or hide* application symptoms when the alert is computed from telemetry the collector transports. For example, span-metrics error rates or missing-series alerts.

**PROPOSED SEMANTIC.** Actors get a declared **domain**:
- `APPLICATION`
- `TELEMETRY` (collectors, agents, exporters, metrics/trace/log backends)
- `CONTROL_PLANE` (API server, controllers, operators, webhooks, DNS)
- `INFRASTRUCTURE` (nodes, storage, CNI, load balancers)
- `UNDECLARED` (the default)

Each incident has a **symptom domain**, derived from the alert's signal and provenance:
- an application SLO alert has domain `APPLICATION`;
- a missing-series, export-failure or pipeline-drop alert has domain `TELEMETRY`.

Domain alone never excludes, contradicts or eliminates anything (I10). It only changes which channels are applicable and which coverage obligations exist. For an actor whose domain differs from the symptom domain, root-ineligibility follows the §4 channel test, with channel O mandatory:
- A `TELEMETRY` actor in an `APPLICATION` incident is root-ineligible only if:
  - channel O is covered: the alert's provenance positively excludes the actor;
  - every other applicable channel is `NO_PATH_COVERED` (for example, no application span shows a synchronous export failure in its request path, and no closure Pod shares a node with the symptom side);
  - the applicable channel set is non-empty (I11);
  - the §4.4 window is covered.
- A `TELEMETRY` actor in a `TELEMETRY` incident is fully root-capable, and D rules may support it.
- `CONTROL_PLANE` and `INFRASTRUCTURE` actors are never excluded in v1, because their channel C and N influence is broad and cannot be positively covered.
- `UNDECLARED` actors get no domain-based consequence. They fall back to Topic A only when they are change hypotheses.

**REQUIRED EVIDENCE.**
- A domain declaration record for the actor (source: §5.2.1).
- The alert's signal provenance: the query or metric source, and the pipeline components it passes through.
- The channel coverage records.

**COVERAGE PRECONDITIONS.** The same as §4, plus a known alert provenance. If the pipeline of the alert's source metric is unknown, channel O is `UNKNOWN`.

**MISSING-EVIDENCE BEHAVIOR.** An undeclared domain, unknown provenance, or any uncovered channel makes the rule `INAPPLICABLE`, and the actor stays in competition. With no domain declaration configured, the otel-collector blockers remain, which is the honest outcome.

**FALSE-RESOLVED FAILURE MODE.** A collector failure that caused the alerting signal to spike or vanish is excluded, and an application actor is resolved instead. Required negative controls:
- (a) A telemetry-loss incident with the collector down: the collector must be root-capable.
- (b) An application alert computed from span-metrics produced by the collector: channel O is a path, so the collector is not excluded.
- (c) An application with synchronous export in its request path, where a collector stall raises latency: channel R is a path, so the collector is not excluded.

**VERSIONED RULE CANDIDATE.** `m21.incident-causal-scope.v1` (proposed):
- reason code `OUT_OF_INCIDENT_CAUSAL_SCOPE` (proposed new code);
- consequence `ROOT_INELIGIBILITY`;
- mechanism `INCIDENT_DOMAIN`;
- audit: actor domain and its source, symptom domain and its basis, and the channel coverage records including O.

#### 5.2.1 Domain declaration source

**Recommendation (OD-B1):** domain comes only from **operator-declared configuration**. That configuration is a versioned mapping from namespace, label selector or workload to a domain. It is included in `rca_config_digest` and applied identically to every incident. Kubernetes labels such as `app.kubernetes.io/component` are *inputs* the operator may reference in a selector. They are never interpreted by the engine on its own authority. No domain is ever inferred from names.

**OWNER DECISIONS (2026-09-27).**
- **OD-B1 — APPROVED.** Domain comes only from explicit operator-declared configuration. Inferring a domain from names is forbidden.
- **OD-B2 — APPROVED.** ITBench may ship one per-application domain declaration for otel-demo. It is frozen before any DEV/TEST use, identical for every scenario, and recorded in the eval config digest. Scenario-specific declarations are forbidden.
- **OD-B3 — APPROVED.** Alert signal provenance comes from an explicit declared mapping. Where it is missing, channel O is `UNKNOWN`.

**AMENDMENT 3 — v1 authority subset.** The frozen domains stay: `TELEMETRY` may carry a conditional exclusion; `CONTROL_PLANE` and `INFRASTRUCTURE` carry no v1 exclusion. Before M21-B starts, the C2 population is measured by domain (`TELEMETRY` / `CONTROL_PLANE` / `INFRASTRUCTURE` / `UNDECLARED`), and the product-value upper bound for B is computed on the `TELEMETRY` subset only. Removing all of C2 in an upper bound overstates B.

### 5.3 Topic C — Asynchronous propagation

**PROBLEM.** A failure that crosses a message broker is invisible to propagation. Kafka and valkey Pods with `DEPENDENCY_ERRORS`, fraud-detection (a consumer), and producers remain unexplained (C4, 7 blockers in 3 incidents).

**CURRENT BEHAVIOR.**
- `classify_runtime_pair` recognizes a `PRODUCER → CONSUMER` parent/child pair.
- `derive_runtime_propagation` counts it as `async_pairs_uninterpreted` and drops it. Only `PAIRED_CLIENT_SERVER` is interpreted.
- Span links are not modeled at all.
- `messaging.system` and `messaging.destination.name` are retained as attributes but never consumed.
- On the dev split, `producer_consumer_pairs = 0`.

**RISK.**
- Treating asynchronous edges like synchronous ones is wrong. A consumer's failure does not propagate back to the producer. A producer's success says nothing about delivery. Destination-level co-occurrence (P writes D, C reads D) is not per-message causation.
- Over-interpretation creates false propagated-effect exclusions of the real root, for example a poison-message producer.

**PROPOSED SEMANTIC.** A messaging destination is a first-class runtime node, identified by `(messaging.system, destination name, broker endpoint where known)`. There are three relation strengths:
- **MSG-1, message-level parent/child:** a `PRODUCER` span is the direct parent of a `CONSUMER` span.
- **MSG-2, message-level link:** a `CONSUMER` span carries a span link to the `PRODUCER` span's context, with the same destination identity. When a message id or correlation attribute is available, it must match.
- **MSG-3, destination-level:** P emitted `PRODUCER` spans to D, and C emitted `CONSUMER` spans from D, in the window, with no per-message correlation.

The semantics by direction:

| Situation | Direction | Consequence |
|---|---|---|
| Producer → broker send failure (PRODUCER span non-success, broker peer identified) | broker → producer (the producer is affected) | May support the broker as an effect *source* via sync-like semantics, only with an exact broker binding |
| Consumer processing failure on a message whose producer span carries an error or poison marker, with MSG-1/MSG-2 | producer → consumer | Async propagated-effect candidate for the consumer (below) |
| Consumer failure with a successful producer | none established | The consumer stays root-capable (local fault or data issue). The producer is *not* excluded. |
| Consumer lag or silence | undetermined | Neutral. Absence of messages is never propagation evidence. |

**Async root-ineligibility (async propagated effect)** requires all of:
- MSG-1 or MSG-2 (never MSG-3);
- explicit non-success on the consumer span, and a positively identified upstream abnormal condition on the producer span or broker;
- causal ordering: producer end ≤ consumer start;
- exact verified Kubernetes bindings on both ends (as in the sync rule);
- no source-capable initiating Finding in the consumer's episode;
- mixed evidence stays `UNKNOWN` (the P2G-5 semantics).

MSG-3 may only count as an open channel M for Topics A and B (I2). It never excludes anything.

**REQUIRED EVIDENCE.**
- For each span: span kind, the messaging attributes (`messaging.system`, `messaging.destination.name`, the operation, and a message or conversation id where emitted), `service.name`, trace and span ids, parent id, and span links (trace and span id of the linked context).
- Exact bindings for both ends.

**COVERAGE PRECONDITIONS.**
- Both producer and consumer are instrumented with messaging semantics in the window.
- The trace reads are complete for the window.
- The links were retained by ingestion. This is an M20.5 normalizer requirement: preserve span kind, messaging attributes and links. M20.5 must not interpret them.

**MISSING-EVIDENCE BEHAVIOR.** Missing links, missing messaging attributes, an uninstrumented consumer (as with fraud-detection) or an uninstrumented broker make the rule `INAPPLICABLE`. The actors stay in competition.

**FALSE-RESOLVED FAILURE MODE.** A consumer that is the real root (a bad deploy of the consumer) is excluded as an async effect because a coincidental upstream error existed. Required negative controls:
- (a) Consumer image change with a healthy producer: the consumer is not excluded.
- (b) Producer and consumer related only at destination level (MSG-3): nothing is excluded.
- (c) Broker outage: the producers' send failures do not exclude the broker.

**VERSIONED RULE CANDIDATE.** `m21.async-propagated-effect.v1` (proposed):
- reason code `ROOT_CAUSE_INELIGIBLE_ASYNC_PROPAGATED_EFFECT` (proposed new code; the synchronous code keeps its exact meaning per I7);
- consequence `ROOT_INELIGIBILITY`;
- mechanism `ASYNC_PROPAGATION`.

**OWNER DECISIONS (2026-09-27).**
- **OD-C1 — APPROVED.** Async uses a separate rule and a separate reason code, `ROOT_CAUSE_INELIGIBLE_ASYNC_PROPAGATED_EFFECT`.
- **OD-C2 — APPROVED.** MSG-3 never carries exclusion authority. It is only a structural or supporting relation (an open channel M).
- **OD-C3 — APPROVED.** Broker-root semantics are outside v1.
- **M20.5 boundary.** M20.5 only preserves provenance: span kind, messaging attributes, links, trace and span ids, timestamps and service identity. All causal interpretation belongs to M21.

**AMENDMENT 3 — async-class safety witness.** Being in the async/untraced class never implies root-ineligibility: in one dev incident the ground-truth root is an async-class manifestation-only Pod. An equivalent synthetic negative control (an async-class actor that is the true root must not become root-ineligible) is part of the M21-C gate. Topic C excludes only under MSG-1/MSG-2, an upstream abnormal condition, downstream non-success, exact bindings, causal ordering and no source-capable initiating evidence in the downstream episode.

**AMENDMENT 3 — C5 is not a rule.** The C5 label never becomes a production predicate; absence of propagation is not a propagated effect. Before any C5 work, a semantic feasibility audit sorts each C5 blocker into exactly one bucket:
- exact incoming sync propagation exists → the existing m16 propagated-effect authority;
- runtime failure origin proven → D2 `ROOT_SUPPORT` only, never rival elimination;
- explained by another supported root → D3 territory (deferred);
- none of the above → remains `UNRESOLVED`.

### 5.4 Topic D — Positive root support

**PROBLEM.** Today the only way to `RESOLVED` is to eliminate every rival. Positive support is a single implicit predicate: a `PATH`/`DIRECT` explanation plus an actor-aligned initiating change gives `SUPPORTED`. It has no rule id, no preconditions record and no evidence scoping. Actors with real failure-origin evidence but no change (C5: payment, product-catalog, recommendation, cart, currency; C3: flagd-config) can only be `UNRESOLVED`. Separately, a manifestation actor must never be eliminated merely because it lies downstream.

**CURRENT BEHAVIOR.**
- `assess_hypothesis` returns `SUPPORTED` if and only if the explanation is `PATH`/`DIRECT` and an initiating Finding of a change kind is present.
- Dominance compares evidence-shape keys between two `SUPPORTED` hypotheses.
- No trace-derived support exists. The boundary classes `REMOTE_NON_SUCCESS_PROPAGATED`, `CALLER_NON_SUCCESS_WITH_SUCCESSFUL_CALLEE` and similar are computed but support nobody.

**RISK.**
- A support rule that also resolves would break "missing proof does not exclude". Support for A says nothing about an unexcluded rival B.
- A "support score" would re-introduce ranking as authority.
- Failure-origin evidence locates *where* a failure appears, not *why*. A payment Pod that returns errors because of a flag value has origin evidence in payment, while the root is the flag configuration.

**PROPOSED SEMANTIC.** Support is a set of versioned rules, each evaluated per hypothesis to `FIRED`, `NOT_FIRED` or `INAPPLICABLE`, carrying:
- `rule_id` and `rule_version`;
- the exact actor (instance UID where applicable);
- exact evidence ids;
- the temporal and identity precondition results.

A hypothesis is `SUPPORTED` if at least one support rule `FIRED` and it has no hard contradiction. **Support never excludes a rival and never resolves on its own.** The resolver's `RESOLVED` conditions (§1) are unchanged. An actor is never made ineligible merely because it lies downstream of something (m16 §9).

Proposed v1 rules:
- **D1 `m21.support.change-onset-path.v1`.** This is the current implicit predicate, made explicit with no behavior change:
  - an actor-aligned initiating change Finding exists;
  - its causal time is `NOT_LATE` against onset + grace;
  - explanation `PATH` or `DIRECT`, with the path hops recorded.
  - Its introduction must leave every diagnosis's epistemic digest **exactly** equal (OD-D3). The D1 support record is therefore an audit field outside the epistemic digest. M20.1b base replay verifies the equality.
- **AMENDMENT 3 — D1 clarification.** `causal_explanation = PATH` does not always mean a path to the incident symptom; a grouped member's path can produce it. D1 stays an exact-behavior audit refactor with no digest change, but a D1 support record gives **no immunity** against a Topic A exclusion. Tightening the D1 predicate is a D1.1 / new contract.
- **AMENDMENT 3 — D2 vs D3.** D2 applies to traced runtime actors only; it cannot support a configuration root such as a ConfigMap. Ground-truth roots that are unsupported configuration changes need D3 (configuration-dependency semantics), which stays deferred and needs its own contract, now with a measured justification.
- **D2 `m21.support.runtime-failure-origin.v1`** (new). The actor is the **origin** of non-success in traced requests. All of these must hold:
  - It is the callee in ≥ 1 `REMOTE_NON_SUCCESS_PROPAGATED` pair.
  - Within the same traces, all of the actor's own traced outgoing calls succeeded, or there were none and the actor is instrumented.
  - The first non-success is after the last healthy observation of the same operation. This is the onset alignment. The healthy baseline is required: an actor that was always failing is `INAPPLICABLE`.
  - The binding is exactly verified.
  - Coverage: the actor emitted spans across the window, and trace reads completed.
  - Consequence: `ROOT_SUPPORT` of kind `FAILURE_ORIGIN`. This establishes *failure origin within the actor's boundary* (its code, config or untraced dependencies). It is support, not proof of initiation. It does not participate in dominance (OD-D1).
- **D3 (deferred).** Explained manifestation, or configuration-dependency influence. A supported change on X that X's consumer Y reads (for example flag configuration → flagd → payment) would *explain* Y's origin evidence. That would make Y root-ineligible as `EXPLAINED_BY_SUPPORTED_ROOT`. This is an exclusion by positive explanation. It needs its own contract (OD-D2) and is **not** authorized here.

**REQUIRED EVIDENCE.**
- D1: the change Finding, the journal versions and the path hops.
- D2: the exact span pairs (evidence ids), the actor's outgoing pairs in the same traces, the healthy-baseline observation ids, the binding verification ids and the trace coverage record.

**COVERAGE PRECONDITIONS.**
- D2 requires the actor to be instrumented across the whole window and the trace reads to be complete.
- An outgoing call to an uninstrumented dependency makes D2 `INAPPLICABLE`: the failure may come from beyond the traced boundary.

**MISSING-EVIDENCE BEHAVIOR.** A rule that cannot be evaluated is `INAPPLICABLE` and records why. `NOT_FIRED` is recorded when all inputs are covered and the predicate is false. Neither changes eligibility, and neither changes the hypothesis's existing epistemic state: a hypothesis already `SUPPORTED` (for example by D1) stays `SUPPORTED`. A hypothesis with no fired rule remains `UNRESOLVED` exactly as today.

**FALSE-RESOLVED FAILURE MODE.**
- A D2-supported actor is resolved while the real initiating change elsewhere was excluded by a weak rule. D2 cannot resolve by itself, so any false resolution must come from an A/B/C exclusion. For that reason, the M21 gate attributes every `RESOLVED` to the exclusions that enabled it.
- A second mode: D2 support feeding dominance could let a failure-origin actor dominate the actual changed actor.

Required negative controls:
- (a) A flag-config change causing payment errors must not resolve to payment.
- (b) An always-failing actor gives D2 `INAPPLICABLE`.
- (c) An actor calling an uninstrumented dependency gives D2 `INAPPLICABLE`.
- (d) **D2 digest visibility.** For a hypothesis already `SUPPORTED` by D1, D2 `NOT_FIRED` gives digest A and D2 `FIRED` gives digest B, and A ≠ B.
- (e) **D1 migration.** The epistemic digest before the D1 audit refactor equals the digest after it, exactly.

**VERSIONED RULE CANDIDATE.** `m21.support.change-onset-path.v1` and `m21.support.runtime-failure-origin.v1`, both with consequence `ROOT_SUPPORT` (a new consequence kind). Support records are persisted in the hypothesis audit. The D1 record stays outside the epistemic digest (exact-equality gate). D2 records are persisted **and included in the epistemic digest** as a canonical, versioned support consequence, independently of the aggregate `HypothesisEpistemicState`. The canonical shape (detail fixed by the implementation task) contains:
- `rule_id` and `rule_version`;
- the hypothesis key or exact actor identity;
- `consequence = ROOT_SUPPORT`;
- `support_kind`;
- the decisive evidence ids.

**Digest invariant.** A difference between D2 `FIRED` and D2 `NOT_FIRED`/`INAPPLICABLE` must be observable in the epistemic digest, even when the hypothesis was already `SUPPORTED` for another reason, for example D1. Otherwise replay could miss a D2 divergence.

Summary:
- D1 record: persisted audit; explicitly excluded from the epistemic digest; exact-equality migration gate.
- D2 record: persisted; included in the epistemic digest as its own consequence.

**OWNER DECISIONS (2026-09-27).**
- **OD-D1 — APPROVED.** D2 does not participate in dominance. Dominance stays change-shape-based.
- **OD-D2 — APPROVED.** D3 (explained manifestation) is deferred and needs its own contract.
- **OD-D3 — APPROVED.** D1 is an audit-only refactor, gated by exact epistemic-digest equality. It is the first M21 implementation step.
- **Frozen boundary (I13).** support ≠ elimination; support ≠ resolution authority. A D2 `FIRED` outcome never eliminates a rival and never produces `RESOLVED` by itself.

## 6. Decision matrix

| Topic | Automatic exclusion? | Positive support? | Missing-evidence behavior | M21 implementation |
|---|---|---|---|---|
| A — unrelated change | Conditional: `ROOT_INELIGIBILITY` only when the applicable channel set is non-empty and every applicable channel is `NO_PATH_COVERED` over the full window | No | Neutral: `INAPPLICABLE`, stays in competition | Yes: `m21.unrelated-change.v1` |
| B — telemetry, control-plane and infrastructure actors | Role-aware, never domain alone (I10): only a declared cross-domain actor with channel O and every applicable channel covered. Control-plane and infrastructure actors never in v1. | Conditional: via D rules when the actor's domain equals the symptom domain | Neutral | Yes: `m21.incident-causal-scope.v1` plus the domain config |
| C — async propagation | Async propagated-effect eligibility with MSG-1/MSG-2 only; MSG-3 never | May follow later (the broker as origin), not v1 | Neutral | Yes: `m21.async-propagated-effect.v1` plus normalizer preservation (M20.5) |
| D — positive root support | No | Yes: versioned rules D1, D2; FIRED / NOT_FIRED / INAPPLICABLE; no scores | Neutral; existing epistemic state remains unchanged | Yes: D1 (audit refactor) then D2; D3 deferred |

## 7. Implementation order and gate (after freeze, after M20)

Order: the M20 live-parity tasks first (owner-frozen order). The M21 steps then follow:
1. D1 audit refactor, gated by exact epistemic-digest equality.
2. The channel catalog, applicability and coverage record (§4).
3. A.
4. B plus the domain configuration.
5. C, which requires M20.5 link and attribute preservation.
6. D2.

Each rule is its own task with negative controls, and each is `INAPPLICABLE`-safe by construction.

The M21 final gate reports these counts, before → after:
- `M21-deferred semantic blockers`;
- `RESOLVED`;
- false resolved (must be 0);
- wrong resolved actor (must be 0);
- previously-correct regressions (must be 0).

It also reports the attribution of every new `RESOLVED` to the exact exclusion and support records, and the §5 negative controls. Thresholds, workloads and rules are never tuned to pass the gate. The test split runs once per tagged release.

**AMENDMENT 3 — order and gate.** The order is no longer derived from blocker counts:

```text
D1
→ channel framework (catalog, applicability, coverage)
→ A v1, including the off-symptom SUPPORTED initiated cohort (R)
→ B v1, contract-authorized TELEMETRY subset only
→ MEASURE: actual correct RESOLVED and safety on the real resolver
→ C5 semantic feasibility + D2 feasibility + MSG-1/MSG-2 availability audits
→ C and/or D2, as the audits justify
→ D3 as a separate contract, only if measurement justifies it
```

The resolver outcome is measured after every value tranche. The frozen scoreboard, reported at every M21 step:
- false `RESOLVED` = 0;
- ground-truth root incorrectly eliminated = 0;
- replay = PASS;
- correct `RESOLVED` = N, remaining `AMBIGUOUS` = 10 − N (dev split);
- C1, C2 and C5 blockers (full census, §2.1);
- leader-is-ground-truth as a secondary diagnostic only.

## 8. Amendment rule

If implementation finds that the code or data semantics conflict with a clause, it stops with `CONTRACT_AMENDMENT_REQUIRED`. The report names the clause, the contrary evidence, the minimal proposed amendment and the gate impact. Only an owner-approved, recorded amendment may change `m21.v1`.

## 9. Amendment record

| Date | Amendment | Source |
|---|---|---|
| 2026-09-27 | Amendment 1 (owner `CONTRACT_AMENDMENT_REQUIRED`). Channel applicability separated from coverage (§4.2a). Exclusion rule restated over applicable channels with an empty-set guard (§4.3). OD-A1 replaced by structural shared-node applicability. Invariants I10–I13 added. Noisy-neighbor, vacuous-truth and applicability-is-not-evidence negative controls added. OD-A2 excludes `Secret`. OD-A4 is bounded to strict, windowed, identity-resolved, coverage-qualified neighbors. The other 10 decisions approved. D1 gate set to exact epistemic-digest equality. | Owner review of `m21.v0` |
| 2026-09-27 | Amendment 2 (owner `CONTRACT_AMENDMENT_REQUIRED`). I12 changed from structural-only to evidence-independent structural or declarative applicability, consistent with I10, OD-A3 and OD-B1. D2 support consequences are included in the epistemic digest independently of the aggregate state (digest invariant), and a D2 digest-visibility negative control is added. The D1 migration control is restated unchanged. Decision matrix D reads "existing epistemic state remains unchanged". | Owner review of amendment 1 |
| 2026-09-28 | Amendment 3 (owner `CONTRACT_AMENDMENT_REQUIRED`, after the M20.6 upper-bound and census measurements). §2.1 full census (111 dev; 54 was an audit-truncation bug) and the R cohort; Topic A applies to SUPPORTED initiated changes with no D1 immunity (R is a cohort, not a rule); B's v1 value measured on the TELEMETRY subset only; C5 is not a predicate and needs a semantic feasibility audit; async-class safety witness in the C gate; D2 vs D3 split; order and scoreboard restated with a real-resolver measurement after every tranche. | Owner review of the M20.6 upper-bound measurement |
| 2026-09-28 | Amendment 4 (owner `CONTRACT_AMENDMENT_REQUIRED`). §10 incident onset: V0 and V1 rejected; V2 approved as the bounded production direction with an explicit alert-channel coverage boundary (snapshot: first alert capture time; live: latest contiguous alert-observation segment, persisted and replayed exactly); causal onset separated from reference time, with UNKNOWN when no qualified episode exists; VI rejected; L3 deferred; Scenario-31 recorded as a benchmark/evidence/ground-truth conflict with no accommodation; engine 1.3.0; committed before its read-only remeasurement. | Owner review of the onset measurements and the S31 forensic audit |
| 2026-09-28 | Amendment 4 result (owner decision after the gate FAIL): 'S24 regression fixed' failed; S24 recorded as a second benchmark/evidence/ground-truth conflict; A4-V2 ships on its safety gates; §10.1–10.3 unchanged (§10.4). | Owner decision on the A4 remeasurement |
| 2026-09-28 | Amendment 5 (owner `CONTRACT_AMENDMENT_REQUIRED`, three review rounds). §4.5 `m21.k-reference-coverage.v1`: K `NO_PATH_COVERED` only under K1 continuous journal coverage, K2 per-kind profile COMPLETE (all six v1 kinds start INCOMPLETE), K3 namespace separation, K4 policy-unbounded traversal over all window versions, K5 no known unsupported reference surface, K6 API surface positively covered by complete Kubernetes audit evidence (permission is only potential; no negative operator assertion; exact kubelet attribution; collection requests; pre-window watches; audit-loss counter continuity; fixed-point closure expansion; impersonation provenance). Preemption and control-plane identity access move to channel C applicability. Every precondition is recorded; deterministic state precedence. Expected on current data: `NO_PATH_COVERED = 0`, accepted as fail-closed. | Owner review of the bounded-declaration draft |

## 10. Incident onset semantics and benchmark evidence conflict (amendment 4)

**Status.** Onset research is closed. The qualified new-diagnostic-episode onset (V2) is the bounded production fix, with the alert-channel coverage boundary defined below. Scenario-31 remains a documented benchmark/evidence/ground-truth conflict. This section fixes a measured safety problem; it adds no causal rule.

### 10.1 Findings (35 ITBench-Lite scenarios, read-only, `.local/m20-audit/m20_6b/`)

| Candidate | Decision | Evidence |
|---|---|---|
| V0 — global `min(activeAt)` of non-background alerts | **REJECTED** | A chronic, cause-type alert sets the onset in 19/35 scenarios, and the onset precedes the evidence window in 24/35. `m16.temporal-contradiction` then eliminates the ground-truth root in 2/35 (S24, S31). |
| V1 — suspend temporal contradiction whenever the onset is unanchored | **REJECTED** — excessive authority loss | 24/35 onsets are unanchored, so most legitimate temporal eliminations are suspended (ceiling +C5: 11 → 6). |
| V2 — qualified new diagnostic episode | **APPROVED** as the bounded production direction | Best measured candidate. That measurement used the object channel as its boundary, which is epistemically wrong (§10.2 rule 7). Its numbers are historical comparison only and are **not** a regression target. |
| VI — 5-minute interval `[L, U]` | **REJECTED** | U = L in 12/35; S31 is still eliminated; ceiling +C5 13 < 14. |
| L3 — telemetry-derived onset | **DEFERRED** to live-product work | ITBench traffic is pod resource-request metrics (no request SLI) and traces are truncated; a change point is found in 4/35. |

**Scenario-31: `BENCHMARK_EVIDENCE_GROUND_TRUTH_CONFLICT`.** The onset-defining `RequestErrorRate` (frontend-proxy, rule "error rate > 0") belongs to a chronic, flapping series that starts before the recorded fault. The recorded NetworkPolicy spec is `ingress: [{}]` (allow-all), and no symptom attributable to it is observable (forensic verdict UNKNOWN). No scenario-specific accommodation is made. S31 may remain a grader mismatch.

### 10.2 Production contract

1. Each source exposes an explicit **alert-observation coverage boundary** W (`alert_observation_start()`), specific to the alert channel.
2. A diagnostic alert whose episode began before W is **pre-existing**: it remains a reported symptom but cannot establish incident onset.
3. The incident **causal onset** is the earliest qualified diagnostic alert episode (non-background) that begins at or after W.
4. If no such episode exists, the causal onset is **UNKNOWN**. No fallback alert timestamp gains causal decision authority:
   - no onset-derived temporal contradiction;
   - no onset-derived positive initiating authority;
   - no onset-dependent A1 elimination.
5. Alert names, scenario ids and ground truth never participate.
6. W is source evidence. It is persisted with the run and replayed exactly, never recomputed.
7. Different evidence channels may have different coverage boundaries. Object/journal coverage cannot substitute for alert coverage.

**Causal onset vs reference time.** `causal_onset` (authority-bearing, may be UNKNOWN) and `observation_reference_time` (for queries, log capture and investigation windows; never authority) are separate values. A V0 timestamp must never be placed in `Symptoms.onset` with an "unanchored" flag; every onset consumer is classified as authority-bearing (reads `causal_onset`) or reference-only (reads `observation_reference_time`).

**Snapshot source.** W = the earliest alert-channel snapshot **capture** time: the timestamp in the name of an alert evidence file (`alerts_at_<t>.json`, `alerts_in_alerting_state_<t>.json`, UTC), not any alert's `activeAt`/`startsAt`. With a single terminal capture, every episode active at that capture is pre-existing, and the causal onset is UNKNOWN. (`scenario.observation_start` is absent in 35/35 scenarios and is not used.)

**Live source.** W = the start of the **latest contiguous alert-observation coverage segment** containing the run boundary, not the time the alert system was first installed. For example: receiver starts 10:00, heartbeat 12:00, gap 12:05–12:20, healthy again 12:20, diagnosis 14:00 → W = 12:20. Alert state during a gap is unknown. The run boundary persists at least:
- `alert_observation_start` (W);
- `alert_observation_end` / last heartbeat;
- `coverage_segment_id`;
- `coverage_status = CONTIGUOUS`.

It must never be inferred from the object journal start, the snapshot start, `min(alert.starts_at)`, `incident_window()` or the current clock. Alertmanager re-sends firing alerts with their original `startsAt`, so chronic alerts that predate the segment are naturally pre-existing.

**Replay and overlay.** Replay uses the persisted boundary exactly, so the same boundary gives the same onset, the same evidence and the same diagnosis digest. An investigation overlay never changes the base run's boundary.

**Engine version.** `RCA_ENGINE_VERSION` 1.2.2 → 1.3.0: on the same evidence, temporal roles, epistemic states, eliminations and resolutions can change. Runs recorded under 1.2.2 replay as UNSUPPORTED under 1.3.0 and are never silently treated as equivalent.

### 10.3 Measurement protocol and gate

This amendment is committed **before** the read-only remeasurement. If the remeasurement does not satisfy the gate, the result is reported as FAIL; the contract is not changed to pass it. The scoreboard compares V0, old V2 (historical only) and A4-V2 over the 35 public scenarios, with S11, S24 and S31 reported separately:
- ground-truth state (supported, unresolved, contradicted, absent);
- correct and wrong `RESOLVED`, `AMBIGUOUS`, `INSUFFICIENT`;
- temporal eliminations, A1 eliminations and initiating supports;
- causal onset `ANCHORED` / `UNKNOWN`, and the pre-existing episode count.

Gate:
- new evidence-supported bad ground-truth elimination = 0;
- engine wrong `RESOLVED` = 0;
- S24 regression fixed;
- no scenario-specific logic;
- deterministic, replayable onset.

Performance numbers are read after the gate, never as it.

### 10.4 Remeasurement result and owner decision (2026-09-28)

The frozen remeasurement (`.local/m20-audit/m20_6b/a4_remeasure.*`) **failed** one gate: "S24 regression fixed". Every other gate passed:
- new evidence-supported bad ground-truth elimination 0;
- engine wrong `RESOLVED` 0;
- no scenario-specific logic.

Results for A4-V2:
- ground truth supported 18 (V0 14), eliminated 1 (S24; V0: S24 and S31);
- temporal eliminations 23 (V0 64);
- causal onset UNKNOWN 1/35 (S11, single terminal capture);
- S31 no longer eliminated, as a consequence of the boundary, not a rule.

In S24 the alert channel is observed from 15:23:59 and records new diagnostic episodes from 15:41:08 (pending pods, request errors, no-requests on six services, crash loops), two hours before the recorded fault (17:52:10). The earlier V2 "fix" of S24 came only from its object-channel boundary hiding these observed episodes.

**Owner decision (after the result, recorded as such):** S24 is a second `BENCHMARK_EVIDENCE_GROUND_TRUTH_CONFLICT` (the environment is observed degraded before the recorded fault). No scenario-specific accommodation is made. A4-V2 ships on its safety gates. The contract text above is unchanged.

## 11. Temporal relevance of supported leaders (amendment; adopted for presentation 2026-10-03, `W` = 5 min)

**Measured problem.** In the ten-hour product-mode run (`testbed_longrun_run1`) 252 of 300 diagnoses showed
competing leaders, every one of them in the `SUPPORTED` tier. Their 561 candidates were mostly old at the incident's
onset (latest initiating finding: 194 within 15 minutes, 184 between 15 and 60, 183 older), and the score did not
separate them: a configuration change one minute before the onset and an experiment that had recovered 90 minutes
before scored the same. Where the incident began during an injected fault and exactly one candidate was recent, that
candidate was the injected cause in 104 of 104 cases. Those numbers come from one run with a fault every 45 minutes
and a 15-minute cut chosen after looking at the data; they motivate the rule, they do not set it.

**Principle.** A `SUPPORTED` claim is never removed or invalidated for being old: its hypothesis, findings and
evidence stay as they are. What changes is only which `SUPPORTED` candidates are **eligible to lead**: a candidate
whose effect ended before the incident began yields leadership to a candidate that is still in effect, connected
in time to the onset and causally linked to the incident. Age alone never demotes; recency alone never promotes.

**Definitions** (`T0` is the incident's causal onset of §10.2; `W` is the connection window):

1. *Effect interval* of a candidate, from observations only:
   - a chaos experiment: `[Applied, Recovered]`; without an observed `Recovered` it is open;
   - a specification change: from the change to an observed later change that restores the earlier value of the
     same field (a revert); without an observed revert it is open;
   - any other actor: open unless an end is observed. An unknown end is treated as **still in effect**.
2. *Ended before onset*: the interval has an observed end and `end + W < T0`.
3. *Connected*: the interval does not begin after `T0` (one second of timestamp resolution tolerated) and either
   contains `T0` or ends no more than `W` before it. This is the onset connection that
   `m21.support.observed-fault-execution.v1` already uses for strong authority, with `W = 5 min`; here it is reused
   as a definition, not changed.
4. *Linked*: the candidate's hypothesis has at least one linked symptom or causal path to a declared incident symptom
   (`linked_symptoms` / `causal_paths` non-empty).
5. *Displacing* (amended 2026-10-02, owner-approved after the shadow measurement): a candidate may make others
   ineligible only from an **observation** that ties it to the onset: an initiation observed within `[T0 − W, T0]`,
   or an execution observed in progress at `T0` (an experiment `Applied` at or before `T0` with no `Recovered` before
   it). An unknown end keeps a candidate eligible (definition 1) but never lets it displace others: an old change
   whose end was simply not observed is not evidence of a connection to this incident.

**Rule (eligibility filter on `SUPPORTED` leadership).** Among the `SUPPORTED` candidates that leader selection
(§ "Leader selection by epistemic tier") would present as competing: if at least one candidate is displacing,
linked and not ended before onset, every candidate that ended before onset is not eligible to lead, and the leader
is chosen among the eligible ones exactly as today (score, then canonical name). If no candidate is displacing,
linked and not ended, nothing changes. The filter never produces `NOT_ESTABLISHED`, never touches the `STRONG` tier,
`RESOLVED`, admission, eliminations or any support rule, and does not change
`m21.support.observed-fault-execution.v1`.

**Not decided here.** `W`. The shadow measurement compares `W` = 5, 15 and 30 minutes; none is adopted until a
value is supported by data independent of the run that motivated it.

**Shadow measurement (before any decision).** Applied offline to stored diagnoses, without changing the engine:

- data: the scored testbed slices (ground truth from the harness), the two product-mode runs with the injector's
  record as truth (`testbed_longrun_run1`, `testbed_longrun_verify2`), and the 35 ITBench scenarios;
- reported per `W` and per dataset:
  1. true-cause retention: where the true cause was a candidate, is it still eligible;
  2. wrong-candidate removal: candidates made ineligible that are not the true cause;
  3. `COMPETING` → `SINGLE` changes, and whether the single leader is the true cause;
  4. `NOT_ESTABLISHED`: must not increase (by construction it cannot; checked anyway);
  5. ITBench: every scenario whose leader or display changes, with the before and after;
  6. incidents with no injected fault (no ground truth): how the display changes, reported apart and never scored;
- compared explicitly with the existing 5-minute onset connection of the strong rule: which cases each one decides.

Adoption, the value of `W`, and whether the filter belongs to the engine's selection (and so to the epistemic
digest) or to the presentation only, are decided after the measurement.

*Shadow measurement (2026-10-02, `packages/evals/temporal_relevance.py`).* Truth: the harness's chain for the slices,
the injector's record for the two product-mode runs, the published labels for ITBench. `NOT_ESTABLISHED` never
increased and the true cause was never made ineligible, at any `W`.

| Dataset | Competing during a fault | → `SINGLE` (W = 5 / 15 / 30 min) | Leader is the true cause | True cause demoted | No-fault competing changed (W = 5 / 15 / 30) |
|---|---|---|---|---|---|
| Testbed slices 1–3, 120 diagnoses replayed with the current engine | 0 | — | — | 0 | — |
| Product-mode run, 10 h, 300 diagnoses | 210 | 186 / 186 / 186 | all | 0 | 0 / 8 / 20 of 42 |
| Product-mode run, 2 h after the fixes, 50 diagnoses | 30 | 30 / 30 / 12 | all | 0 | 0 / 4 / 4 of 4 |
| ITBench, 35 scenarios | 2 competing, true cause not among the candidates | no change at any `W` | — | 0 | — |

Readings. The filter decides only where several experiments overlap in the incident window (the long runs); the
slices (one fault per clean run) and ITBench have nothing for it to do, and nothing changes there. `W = 5 min`, the
strong rule's existing onset connection, gave the cleanest result: every decision it took was the true cause and it
left every incident without ground truth untouched; larger windows began to change those (8 and 20) and, once wider
than the 20-minute fault spacing of the second run, lost most of their effect (12 of 30). The strong rule itself
decided 2 of the 470 testbed diagnoses; the two rules act on different cases. An earlier count that put half of the
252 competing diagnoses outside any fault was wrong: the harness records a rollout's creation and removal under
different texts, and the first analysis did not pair them; 42 of the 252 fall outside a fault.

Open before a decision: the benefit is measured on the same two runs that motivated the rule; a run with a
different fault rhythm, or the held-out set, is the independent check. The first version let a candidate of
unknown end make others ineligible; definition 5 now requires an observed initiation within `W` or an observed
execution in progress. Rerun with definition 5, every number in the table above is unchanged: in these data every
candidate that displaced others had in fact begun within the window.

### 11.1 Independent check: pre-registration (owner-approved 2026-10-02, frozen before the run)

Written and committed before the run starts; nothing below changes after its data is seen.

**Run.** Product mode, three hours, `packages.evals.live.longrun` with tag `indep1`. The schedule is generated from
a seed by `rhythm_schedule` and frozen in `.local/longrun/indep1/schedule.json`: seed **20261003** (the first seed
from 20261002 whose schedule satisfies `schedule_problems`), sha256
`e5ae7b2c9dcca5b567abcdba1d60c63967aaac0e4e46ec5f2922bf6f57dff6d7`. The rhythm differs on purpose
from the runs the shadow was first measured on: gaps from three bands (4–10, 15–25, 35–50 minutes), seeded durations
(60–300 s) and order, the same target hit twice in a row, three overlapping pairs (one with a 20-minute fault still in
effect when the second starts), a quiet stretch of 38 minutes, and one control-plane restart. Fault kinds are the
existing three; no injection code changes. The schedule has 11 faults (the design estimate of 15 to 18 was wrong;
the long bands fill the time).

**Frozen.** The filter as defined above, including definition 5, in `packages/evals/temporal_relevance.py` at the
commit of this section; `W` = 5, 15 and 30 minutes; the truth rule: every fault whose
[created − 1 min, removed + 3 min] contains the incident onset (`truth_at`), so an overlap has more than one true cause.

**Hard criteria** (any violation: the filter is not adopted as it stands, and each case is reported):

1. no true cause demoted;
2. in an overlap, no cause in effect demoted;
3. no `COMPETING` to `SINGLE` with a leader outside the truth;
4. no `NOT_ESTABLISHED` added.

**Descriptive, no threshold:** `COMPETING` to `SINGLE` per `W`; every change in a no-fault diagnosis, examined one by
one; the number decided by the strong rule.

**Choice of `W`, fixed now:** the smallest `W` among 5, 15 and 30 with no hard violation on both the development runs
(`testbed_longrun_run1`, `testbed_longrun_verify2`) and this run. If none qualifies, nothing is adopted. Placement
(engine or presentation) stays the owner's decision after the result.

### 11.2 Independent check: result (2026-10-02)

Run `indep1` (`testbed_longrun_indep1`) completed as scheduled: 11 faults, 41 incidents, 82 diagnoses (78
`COMPETING`, 4 `SINGLE`), no change-stream gap, one control-plane restart. Measured once, after the run, with
`longrun measure`; the development runs were measured with the same code.

| Run | `W` | Hard violations | `COMPETING` → `SINGLE` (fault) | Changes without a fault |
|---|---|---|---|---|
| `indep1` (independent) | 5 | **none** | 63 of 78 | none (no incident outside a fault) |
| `indep1` | 15 | none | 25 of 78 | none |
| `indep1` | 30 | none | 19 of 78 | none |
| `run1` (development) | 5 / 15 / 30 | none | 186 of 210 at every `W` | 0 / 8 / 20 of 66 |
| `verify2` (development) | 5 / 15 / 30 | none | 30 / 30 / 12 of 30 | 0 / 4 / 4 of 8 |

At `W` = 5 in `indep1`, by the number of true causes at the onset:

- one true cause in the candidate pool: 49 to `SINGLE` on it, 1 left `COMPETING`;
- two true causes (an overlap), both in the pool: 10, all left `COMPETING` with both eligible; neither was demoted;
- two true causes, only one in the pool: 14 to `SINGLE` on that one, 4 already `SINGLE`; the other cause was never a
  candidate, so the filter cannot have removed it;
- one true cause not in the pool: 4, left `COMPETING`; the engine's pool missed the cause, which the filter neither
  hides nor repairs (an engine finding for C2 or C5, not examined here).

**Choice of `W` by the pre-registered rule:** 5 minutes, the smallest with no hard violation on both development
runs and the independent run; it is also the only one of the three that changed no diagnosis outside a fault. The
quiet stretch of `indep1` produced no incident, so the independent run says nothing about no-fault behaviour; that
evidence comes from the development runs alone. Adoption and placement (engine or presentation) are the owner's
decision.

### 11.3 Adoption for presentation (owner-approved 2026-10-03)

Adopted in the operator's leader projection only, with `W` = 5 minutes (§11.2): the engine's `root_cause`, ranking,
support rules and epistemic digest are unchanged; moving the rule into leader selection itself stays a separate
decision. One implementation serves both the projection and the shadow (`packages/rca/temporal_relevance.py`; the
shadow in `packages/evals` only builds its inputs from stored documents), so what was measured is what is shown.
The projection lists only the eligible supported candidates (`leading_actor_candidates`, `SINGLE` when one remains)
and records the others in `leading_actor_set_aside`, outside the digest; their claims are untouched. A claim without
an onset (a record from before these fields) leaves the filter inactive.

**Check on real data.** The control plane with this change re-diagnosed all 41 incidents of `indep1` on a copy of its
database (`testbed_longrun_indep1_c15`): for every incident the shown display and candidates equal the shadow's
prediction at `W` = 5 from the earlier diagnosis (41 of 41); `root_cause` unchanged in 41, epistemic digest equal in
41; 33 incidents now show one leader and 8 still compete (35 with a candidate set aside).

## 12. Service-level effect relation, revised with latency (amendment, owner-approved 2026-10-03)

Status: **APPROVED** by the owner (2026-10-03) for the shadow measurement of §12.4; adoption follows it. Roadmap C1. It revises the specified, unimplemented "Service-level effect
relation" above, now that live traces exist (`live-trace-design.md`). Nothing below is implemented.

### 12.1 What the traces show

The relation as specified reads only **non-success** call edges. In the lab's trace runs (slices 1 and 3, the base read
of `live-trace-design.md` §9) every `order-service` → `payment-service` call was HTTP 201, before and during the
fault, so it would never hold for the delay and loss families. Their effect is latency, and it is large: the caller's
call duration had a median of 4 to 5 ms in the baseline window and **0.86 to 3.3 s** during the fault in five of six
runs. In the sixth (a configuration change) the calls inside the change's interval were still fast, because a rollout
slows calls only once its new pod serves them: the interval that matters is the execution's, not the change's.

### 12.2 Relation

For an execution witness at a fault target `T` (a Pod, with its execution interval `[start, end]`: for chaos
`[Applied, Recovered]`; for a rollout, from its first new pod serving, defined with C5) and a declared incident symptom
service `S`, the **service-level effect** holds when either form holds, with a `VERIFIED` binding at both ends:

1. **Non-success** (as specified): a direct paired call edge `S` (client) → `T`'s service (server) with non-success
   outcomes first observed at or after `start` and not later than `end`, absent before `start`.
2. **Latency** (new): paired calls from `S` (client span) to `T` (server span carrying `T`'s exact pod name) that begin
   inside `[start, end]`, compared with the paired calls `S` → `T`'s service in the run's **baseline window** before
   `start`: at least `N` calls on each side, and the fault calls' median duration above both `F` × the baseline's
   **median** and `D` in absolute terms; a baseline whose median already exceeds `D` leaves the relation unknown
   (amended §12.5).

The server span binds the effect to the **exact pod** of the witness, so an effect on another replica, or on the same
service before the fault, is not this fault's. A call-graph path alone remains no effect. An incomplete trace read
(`TRUNCATED`, `FAILED`, or no calls on a side) leaves the relation **unknown**, never false.

### 12.3 Consequence for the rules

`m21.support.observed-fault-execution.v1`'s incident-effect relation may then hold through the service-level effect, for
a target that is not itself a symptom: the witness covers `S`. Strong authority follows only under the rule's other
conditions (onset connection, timing gates); `RESOLVED` still needs every declared symptom covered. No new claim kind,
no change to admission or elimination.

### 12.4 Parameters and measurement (pre-registered before any number is looked at for them)

`N`, `F`, `D` are not fixed here. Shadow first, as for §11:

1. Development: the `DEV` slices re-run with the trace read on; for every candidate `(T, S)` the relation is computed
   offline for a grid (`N` ∈ {3, 5}, `F` ∈ {3, 10}, `D` ∈ {0.2 s, 0.5 s}); reported against the world's chain (the
   propagation links of the ground truth) and against the decoy and every off-chain target.
2. **Hard criteria**: the relation never holds for a target off the chain (in particular the negative control's decoy),
   and never makes a strong claim on an off-chain actor.
3. Choice, fixed now: the **most permissive** grid point with no hard violation on `DEV`; then confirmed once on a new
   `HOLDOUT` with the engine frozen. If none qualifies, nothing is adopted.

### 12.5 Baseline by its median (amendment, owner-approved 2026-10-03)

Found on the `DEV` shadow: in five of the cases where the true target's effect was real, the relation read false
because the baseline window held slow calls that were not this incident's: the runs are back to back and Tempo is
shared, so the window ten to five minutes before an onset can hold the previous run's fault (slice 5 repeat 0: a
3.35 s tail from the scheduled run before it; slice 4b repeat 2: the previous repeat's spawns). A real cluster has the
same exposure to an earlier, unrelated slowdown. The p95 of the baseline is moved by a few such calls; the **median**
is not, unless they fill half the window. The relation therefore compares the fault calls' median with `F` × the
baseline's median, and a baseline whose median is itself above `D` cannot show a rise: the relation is unknown there.

### 12.6 DEV shadow result (2026-10-04)

All six `DEV` families with the trace read on (slices 1 and 3 from `tr4`, slice 2 from `tr5`, slices 4b, 5 and 6 from
`tr6`; every run valid), the relation computed for every witnessed target and symptom service of every incident, with
the baseline by its median (§12.5):

| Truth | Holds / false / unknown, at every grid point |
|---|---|
| target on the chain, symptom of its own cause | **7 / 0 / 16** |
| target off the chain | **0** / 0 / 30 |
| the negative control's decoy | **0** / 0 / 19 |

No hard violation at any grid point; the grid points do not differ on this data. By the rule fixed in §12.4 the
choice is the most permissive: **`N` = 3, `F` = 3, `D` = 0.2 s**, to be confirmed once on a new `HOLDOUT` with the
engine frozen. Limits stated plainly: the decoy is never contradicted, only never reached (no symptom service calls
the isolated workload, so its relation is always unknown, which is its construction); the unknown cases are incidents
whose captured spans held no fault or no baseline calls between that pair of services (for example a lag incident,
whose service never calls the target); the rollout families have no execution witness until C5.

### 12.7 Engine wiring (2026-10-04)

The relation is wired into the fault-execution witness: when the exact target pod of a closed execution interval is
not itself a symptom pod and the interval connects to the onset, each symptom `Service` of the same namespace other
than the target's own service is tested with §12.2 at `N` = 3, `F` = 3, `D` = 0.2 s and the baseline
`[onset − 10 min, onset − 5 min]`. When it holds, a witness `FAULT_EXECUTION_EFFECT_AT_CALLER` names that service as
the symptom (`OBSERVED_MECHANISM_CAUSE`); unknown or false adds nothing. `RCA_ENGINE_VERSION` is unchanged.

Replay of the §12.6 `DEV` incidents with the wired engine against their stored diagnoses: 6 new witnesses, all on the
chain, none off it; 6 leaders `SUPPORTED` → `STRONG`, 2 `COMPETING` → `SINGLE`, 2 root causes moved to the
experiment's `Schedule`; no resolution changed.

ITBench-Lite (35 scenarios, `DEV`): the relation never holds, and this is a limit of the data, not of the rule. 12
scenarios carry closed chaos intervals; in all 12 the recorded executions lie either about an hour before the onset
(last `Applied` 16:40–16:51 UTC, on pods replaced around 17:16, absent from every captured span) or 17–26 minutes
after it (first `Applied` 17:52–18:01, on the current pods). No execution is recorded around the onset (17:26–17:35),
so no interval connects to it; and the captured traces begin only 5–9 minutes before the onset, leaving the
baseline window empty or partial. ITBench-Lite can therefore neither confirm nor contradict §12; the confirmation stays
with the new `HOLDOUT` of §12.6.

### 12.8 HOLDOUT confirmation (2026-10-04)

Confirmed on the fifth testbed `HOLDOUT` (`testbed-scenarios-design.md` §17.1), the engine frozen at `58eadf38`: all 18
runs valid and read their traces without a failed read; eight witnesses of the relation, each naming the run's own
cause, none off the chain. `N` = 3, `F` = 3, `D` = 0.2 s stand. The second and third `HOLDOUT`s could not decide it
(failed trace reads, §13.1 and §14.1); the fourth was stopped (§16.1).

## 13. Rollout execution witness (C5a; proposal, 2026-10-04, awaiting the owner)

### 13.1 Measured problem

On the testbed, the configuration families (`config-or-rollout`, `negative-control`) name their cause, a `Deployment`
change, in every run but never with strong authority: the only execution witnesses are chaos records (§ observed fault
execution) and quota rejections, and a rollout has neither. Execution-witness recall is 0 in every `DEV` and `HOLDOUT`
run of both families (`testbed-scenarios-design.md` §12.8 to §18.1).

A rollout's execution is observable in the change stream, by UID on every link: the `Deployment` change (generation
up), the `ReplicaSet` it creates (owner UID = the `Deployment`'s), and that ReplicaSet's pods (owner UID = the
ReplicaSet's), each with its creation and deletion time. Its effect is observable in the traces: in `DEV` run `tr4-slice3`
#0 the order service's calls to the new pod took 1.22 s at the median against 0.004 s to the old pod before the change.

### 13.2 Rule (`m21.support.observed-rollout-execution.v1`)

1. **Execution witness**, at the exact change: a `Deployment` version whose `metadata.generation` rose, a `ReplicaSet`
   created within 10 s of it whose owner UID is that `Deployment`'s, and each pod created by that ReplicaSet (owner UID).
   The pod's **execution interval** is `[its creation, its deletion]`, or `[its creation, cutoff]` while it lives; it
   must be connected to the onset as in the fault-execution rule (begin no later than the onset, end no earlier than
   5 minutes before it). Names, image tags, revisions and a `ScalingReplicaSet` event alone confer nothing.
2. **Effect relation**: §12.2 unchanged, at the exact new pod: a symptom service's calls to that pod inside its
   interval against the baseline window of the trace read, restricted to calls that began **before the change**
   (calls to the old revision). `N` = 3, `F` = 3, `D` = 0.2 s, as confirmed in §12.8; no new parameter. Unknown or
   false adds nothing.
3. **Carrier**: the `Deployment` claim, which must already be D1-supported; the timing gates apply unchanged. Scope:
   the observed effect at the callers of the new revision; `RESOLVED` stays out of reach as for §12.
4. **Out of scope (C5b)**: a rollout whose new pods never serve (an image that cannot be pulled, a crash) and whose
   effect is the loss of the old revision. Its witness is the new pods' container state and the old ReplicaSet's
   scale-down, its effect an outage at the callers (failed client calls with no server span), and it needs its own
   `DEV` scenario before any rule; the held-out `config-b` (a missing image tag) is that kind and is not used to design it.

### 13.3 Exploratory shadow on `DEV` (run before this text, stated as such)

All six `DEV` trace suites (`tr4-slice1`, `tr4-slice3`, `tr5-slice2`, `tr6-slice4b`, `tr6-slice5`, `tr6-slice6`; 123
incidents), the relation computed for every connected new pod of every `Deployment` change and every other symptom
service:

| Change | Holds | False | Unknown |
|---|---|---|---|
| the run's cause (`payment-service`), symptom `order-service` | **3** | 0 | 3 (no call to the new pod captured) |
| the run's cause, symptom `order-worker` (never calls it) | 0 | 0 | 8 |
| not a cause (`order-service`, the harness's fresh pod) | **0** | 0 | 4 |

No off-chain change holds; half of the cause's pairs are unknown because the fault window of the trace read held no call
to the new pod.

### 13.4 Measurement, pre-registered

Hard criteria as §12.4: the relation never holds for a `Deployment` change off the chain, and no strong claim rests on
one. Confirmed once on a new `HOLDOUT` with the engine frozen; the families whose cause is a serving rollout are
`config-or-rollout` and `negative-control` in variant A, so the `HOLDOUT` adds a **variant C** of both, each through a
blind phase 0. The target stays `payment-service` (only its callers are traced: a change to `order-service` would make
the symptom service the target, which the relation excludes), with mechanisms that variant A did not use:
`config-c` sets `FAULT_PAYMENT_ERROR` (the new revision fails calls: the relation's non-success form), `negative-c` sets
`FAULT_PAYMENT_DB_QUERY_DELAY_MS` (slow through the database) beside the isolated decoy. The other six families run as in §16 of the scenarios design to check that nothing else moves.

### 13.5 Engine wiring and `DEV` replay (2026-10-04, owner-approved)

Wired as `m21.support.observed-rollout-execution.v1` for `Deployment` holders (resolution passes the source's object
history and cutoff; the timing gates count it as an execution rule). The six `DEV` trace suites replayed (123
incidents) against their stored diagnoses: **3 new rollout witnesses, all on the chain** (`tr4-slice3` #2, `tr6-slice6`
#0 and #1, each `payment-service` → `order-service`), **none off it**; with C1's earlier changes the tier moved
`SUPPORTED` → `STRONG` in 9 incidents (6 from C1, 3 from this rule), no resolution changed. Next: the variants C of
§13.4 through a blind phase 0, then a `HOLDOUT` with the engine frozen.

### 13.6 HOLDOUT confirmation (2026-10-05)

Confirmed on the seventh testbed `HOLDOUT` (`testbed-scenarios-design.md` §19.1), the engine frozen at `36e3cb4b`: 24 of
24 runs valid, 23 with every trace read complete; 13 rollout witnesses, all in the variants C and each naming the run's
own change, none off the chain; no false strong authority or false `RESOLVED`. A failed rollout (C5b) stays open.

## 14. Failed rollout execution witness (C5b; proposal, 2026-10-05, awaiting the owner)

### 14.1 Measured problem

§13 needs the new revision to serve: its effect is read from calls to the exact new pod. A rollout whose new pods never
serve (an image that cannot be pulled, a process that cannot start) leaves no such call; its effect is the loss of the
old revision. The held-out `config-b` and `negative-b` (a missing image tag) are of this kind and have had no execution
witness in any `HOLDOUT`. They are not used to design this rule: a `DEV` scenario of its own was added (`config-d`,
`FAULT_PAYMENT_DELAY_MS` set to a value the payment service cannot start with, with the image variant's strategy that
removes the old pod first), one phase 0 and three `DEV` runs, all valid (`dev5b-config-d`).

What those runs show, by UID on every link: the change, its new ReplicaSet and pod; that pod's own `Warning` events
(`BackOff`, "Back-off restarting failed container"); the old revision's pod deleted two seconds after the change; and
in the traces, the order service's calls to `payment-service` (`server.address`) failing with **no server span**
during the outage, against successful paired calls before the change.

### 14.2 Rule (`m21.support.observed-failed-rollout.v1`)

1. **Execution witness**, at the exact change, as §13.2.1 (the change, the ReplicaSet created within 10 s and owned by
   the `Deployment`'s UID, its pods by owner UID), and in addition, for the new pod: a `Warning` event of that pod's own
   UID with reason `BackOff`, `Failed`, `ErrImagePull` or `ImagePullBackOff` inside its life; and a pod of **another**
   ReplicaSet of the same `Deployment` (owner UIDs) deleted within 5 minutes after the change. The execution interval is
   `[that deletion, the new pod's deletion or the cutoff]`, connected to the onset as §13.2.1.
2. **Effect relation**: a symptom service (other than the target's) whose client spans to the target service
   (`server.address`) inside the interval include at least `N` non-success calls **with no server span**, while at
   least `N` of its calls to that service in the trace read's baseline window, before the change, include no
   non-success. `N` = 3 as §12; fewer calls on either side leave it unknown, never false.
3. **Carrier and scope** as §13.2.3: the `Deployment` claim, D1-supported, the timing gates unchanged; the witness's
   path is `rolls_out` to the new pod, then `called_by` to the symptom service. A rollout that keeps some capacity
   (more replicas, a surge) shows no unpaired failures and gets no witness from this rule.

### 14.3 Exploratory shadow on `DEV` (run before this text, stated as such)

The three `dev5b-config-d` runs, its phase 0 and the six `DEV` trace suites (151 incidents):

| Change | Holds | False | Unknown |
|---|---|---|---|
| the run's cause (`payment-service`), symptom `order-service` | **2** | 0 | 5 (no failed call or no baseline call captured) |
| the run's cause, symptom `order-worker` (never calls it) | 0 | 0 | 10 |
| any other change | **0** | 0 | 0 (no other change has a failing new pod and a removed old one) |

### 14.4 Measurement, pre-registered

Hard criteria as §12.4 and §13.4: never holds for a change off the chain, no strong claim rests on one. Confirmed
once on a new `HOLDOUT` with the engine frozen, the `config-b` and `negative-b` variants (a missing image) being the
held-out failed rollouts, beside the other variants as in testbed-scenarios-design §19; scored with the grader of §20
(a `rolls_out` hop to the chain's target pod).

### 14.5 Engine wiring and `DEV` replay (2026-10-05, owner-approved)

Wired as `m21.support.observed-failed-rollout.v1` beside §13 for `Deployment` holders (a claim's execution rules are
read together for strong authority; each keeps its own record in the audit). `DEV` replay (`dev5b-config-d` and the six
trace suites, 147 incidents): **2 new witnesses, both on the chain** (`dev5b-config-d` #2, `payment-service` →
`order-service`), **none off it**; no resolution changed.

### 14.6 Eighth HOLDOUT: not exercised (2026-10-05)

The held-out failed rollouts (`config-b`, `negative-b`) got no witness because the implementation of §13 and §14
accepts only a `SPEC_CHANGE` origin, and an image change is an `IMAGE_CHANGE` (testbed-scenarios-design §21.1). The text
of §13.2.1 ("a version whose `metadata.generation` rose") includes it; the implementation is brought to the text by
accepting every change finding of the `Deployment` that changes its pod template, and C5b is measured again on a new
`HOLDOUT`.

The implementation now accepts `SPEC_CHANGE`, `IMAGE_CHANGE` and `ROLLOUT_RESTART` origins for both rules (a
`SCALE_CHANGE` rolls nothing out). `DEV` replay unchanged: §13 three witnesses, §14 two, all on the chain; the
harness's own restarts are now candidates and give no witness.

### 14.7 HOLDOUT confirmation (2026-10-05)

Confirmed on the ninth testbed `HOLDOUT` (testbed-scenarios-design §22.1), the engine frozen at `ca29eeea`: 24 of 24
valid; the held-out failed rollouts (a missing image) got 12 witnesses in 4 of 6 runs, each naming the run's own change,
none off the chain; no false strong authority or false `RESOLVED`.

## 15. Observed message delivery as a structural path (C3a; proposal, 2026-10-05, awaiting the owner)

### 15.1 Measured problem

A fault on a producer cannot reach a consumer-side symptom: the topology has no producer → consumer relation (roadmap
C3, run `indep1`). On `DEV` the same holds: in `tr5-slice2` (CPU stress on `order-service`), the lag incidents of
`order-worker` (`KafkaConsumerLag`, `OrderWorkerLagHigh`) list the stress experiment only as `UNLINKED` context, and
their root cause is empty.

The traces show the delivery per message. The workload emits no `PRODUCER` span: the order service injects its request
context into the message, so each `order-worker` `CONSUMER` span's direct parent is the producing request's span in
`order-service` (391 of 391 consumer spans on `DEV`). This is Topic C's MSG-1 (message-level parent/child), with the
parent span of the producing request rather than of kind `PRODUCER`.

### 15.2 Relation (`delivers_to`)

1. **Derived from captured spans only:** a `CONSUMER` span whose direct parent (same trace, not a conflicting key) is a
   span of another service, starting no later than the consumer. Each pair adds `delivers_to` edges from the parent's
   endpoints to the consumer's, with both spans as edge evidence.
2. **Endpoints:** the `Deployment` named by the span's binding when verified present at the span's time (as runtime
   propagation), and the `Pod` of the exact name observed present at that time. Runtime propagation also requires the pod
   UID because it can exclude; this relation only opens a path, and the spans carry no pod UID.
3. **Semantics:** forward only, producer → consumer (a consumer's failure never travels back). A **structural path**
   for D1 (a possible initiating cause) and nothing else: it excludes no actor, retires no claim, grants no execution
   witness and no strong authority. Topic C's exclusion rule (`m21.async-propagated-effect.v1`) and its owner
   decisions are unchanged; lag or silence stays neutral for exclusion.
4. **Not covered:** destination-level co-occurrence (MSG-3), span links (MSG-2, none emitted here), a broker as root.

### 15.3 Exploratory shadow on `DEV` (run before this text, stated as such)

Every `DEV` incident (the six trace suites and `dev5b-config-d`, 147) diagnosed with and without the relation: **5
change, all `tr5-slice2` lag incidents**: in 4 the root moves from none to the run's own stress experiment
(`SUPPORTED`, resolution `INSUFFICIENT_EVIDENCE` → `AMBIGUOUS`), in 1 to the same experiment as `UNESTABLISHED`. **No
other incident changes; no root moves to an actor off the chain.** An earlier draft admitted pods only with a UID and
linked nothing (the stress experiment disrupts the pod, which never reached the `Deployment`-level edge).

### 15.4 Measurement, pre-registered

Hard criteria: no incident's root moves to an actor off the chain because of the relation, and no new false strong
authority or false `RESOLVED`. Confirmed once on a new `HOLDOUT` with the engine frozen, the relation on; the held-out
cases are the variants whose cause sits on the producer and raises consumer lag. `scheduled-b` (a `Schedule` stressing
`order-service`) is one; its lag incidents are outside its scored alert set, so the measurement reads every incident of
the run whose alerts are the lag alerts and reports whether its root is the run's own cause, beside the usual scores.

### 15.5 Engine wiring and `DEV` replay (2026-10-05, owner-approved)

On by default (`engine.build_case` adds the relation after runtime propagation). `DEV` replay, the engine with and
without it on the same 147 incidents: the same 5 changes as §15.3, all `tr5-slice2` lag incidents now rooted at the
run's own stress experiment (4 `SUPPORTED`, 1 `UNESTABLISHED`), no other incident changed, nothing off the chain.

### 15.6 Offline regression replay (testbed-scenarios-design §23, 2026-10-05)

Every stored testbed run, nine `HOLDOUT`s and seven `DEV` suites (1,333 incidents, 6 minutes), with and without the
relation: **44 incidents change, all lag incidents** (`scheduled-b` of `HOLDOUT`s 2, 3, 5, 6, 7, 9 and `tr5-slice2`),
the root moving from none, or in four from an `UNESTABLISHED` `payment-service`, to the run's own `Schedule` or one of
its spawned experiments; **no root moves off the chain**. Two `holdout3-competing-b` incidents now lead with
`Deployment/order-service` as `UNESTABLISHED` (a propagation link of the chain, not its cause; shown as not
established). Many `scheduled-b` displays move from `SINGLE` to `COMPETING`: the `Schedule` and its spawned experiment
are both supported, two links of one cause. These runs were seen, so this is a regression check, not held-out evidence.

### 15.7 HOLDOUT confirmation (2026-10-05)

Confirmed on the targeted tenth `HOLDOUT` (testbed-scenarios-design §24.1): 6 of 6 valid, 2 of 30 incidents rooted at
the run's own spawned experiment through the relation, none moved off the chain, no false strong authority or false
`RESOLVED`. A small set, as §23 intends for a targeted run.

## 16. The baseline just before the first execution (C16; proposal, 2026-10-05, awaiting the owner)

### 16.1 Measured problem

§12's baseline is a fixed window of the trace read, `[onset − 10 min, onset − 5 min]`. On `DEV`, with the current
engine, two of the six `dependency-fault` and `scheduled-recurring` runs miss their execution witness because of it:

- `tr4-slice1` #0: the order service's eight calls to the delayed pod took 2.65 s at the median, but the window held
  no call at all (the run's load starts about a minute before the fault), so the relation is unknown;
- `tr6-slice4b` #2: the window held ten calls at 2.4 s from the run before (Tempo keeps every run's traces) beside eight
  at 0.006 s, so its median was already slow and the relation unknown.

A baseline is meant to show the calls as they were just before the fault; the fixed window looks further back than the
fault's own start and ignores it.

### 16.2 Amendment

The baseline is the calls that began in the **five minutes before the first execution** of the holder's fault
(`[first execution − 5 min, first execution)`, the first `Applied` of any of its experiments, or for a rollout the
change itself), read from the trace read's two windows. `N`, `F`, `D` and the median (§12.5) are unchanged; fewer than
`N` calls there leave the relation unknown, as before. §13 and §14 take the same window before their change.

### 16.3 Exploratory shadow on `DEV` (run before this text, stated as such)

The six `DEV` trace suites, every connected execution target and every other symptom service, three baselines:

| Baseline | Holds on the chain | Holds off the chain | Unknown off the chain |
|---|---|---|---|
| fixed window (§12) | 6 | 0 | 39 |
| every call before the first execution | 7 | 0 | 39 |
| **five minutes before the first execution** | **8** | **0** | 39 |

The seven remaining unknowns on the chain have no fault call or no baseline call captured at all.

### 16.4 Measurement, pre-registered (testbed-scenarios-design §23)

The offline replay of every stored run first: no root moves off the chain, no new witness off the chain, no new false
strong authority or false `RESOLVED`. Then a targeted `HOLDOUT` of the variants it reaches (`dependency-b`,
`direct-b`, `scheduled-b`, `config-c`, `negative-c`) with new seeds, under the same criteria as §12.4, §13.4 and §14.4.

### 16.5 Offline regression replay (2026-10-05, owner-approved)

Implemented for §12, §13 and §14. Every stored testbed run (1,333 incidents), the engine on `main` against this one:
**10 new witnesses, all on the chain** (6 of §12, 4 of §14), **2 lost, both on the chain** (one of §12, one of §14:
their old window happened to hold a usable baseline that the five minutes before the change do not), **none off the
chain**; the tier rises `SUPPORTED` → `STRONG` in 10 incidents and falls in 2; one root moves from another actor to the
run's cause; no root moves off the chain, no resolution changes. A regression check on seen runs, not held-out
evidence.

### 16.6 HOLDOUT confirmation (2026-10-05)

Confirmed on the targeted eleventh `HOLDOUT` (testbed-scenarios-design §25.1): 15 of 15 valid, 19 witnesses of §12 and
§13, none off the chain, no false strong authority or false `RESOLVED`, 14 of 15 runs with every trace read complete.
