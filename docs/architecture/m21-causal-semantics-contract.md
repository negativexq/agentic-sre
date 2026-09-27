# M21 Causal Semantics Contract

Contract version: `m21.v0`
Status: **FREEZE CANDIDATE — owner amendment 1 applied (2026-09-27); awaiting owner freeze.** No M21 production rule may be implemented before this document is frozen. On freeze it becomes `m21.v1`.
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

| Channel | Meaning | APPLICABLE iff (structural, §4.2a) | Path evidence (keeps the actor alive) | Coverage evidence (needed to claim "no path") |
|---|---|---|---|---|
| K — Kubernetes reference | Owner/selector/routing/config/secret/volume/env/service-account/network-policy/quota/limit-range/PDB/HPA/scheduling references, including namespaceSelector and cross-namespace references | Always (every object has a reference surface) | Any reference chain from the change closure (§4.2) to a symptom-side entity | Object journal complete for every namespace and cluster-scoped kind in the window, **and** the relation extractor declares complete reference coverage for every kind in the closure (a versioned per-kind table) |
| R — synchronous runtime | Traced CLIENT→SERVER calls | The closure contains a Pod instance that ran in the window, or a Service | Any traced call edge between closure services and symptom-side services, in either direction, in the window | Every Pod in the closure is instrumented (it emitted spans in the window) and trace reads for the window completed without error or truncation |
| M — asynchronous messaging | PRODUCER → destination → CONSUMER | The closure contains a Pod instance that ran in the window | Any message-level or destination-level relation (§5.3) between the closure and the symptom side | Messaging instrumentation present on the closure's producers and consumers, and complete trace reads |
| N — shared-node causal channel | CPU, memory, disk, network or PID contention on a shared node | The closure contains at least one Pod instance that was scheduled and running on a known node during the window (OD-A1) | A closure Pod and a symptom-side Pod shared a node in the window | All relevant placements known for the whole window **and** no closure Pod shares a node with a symptom-side Pod. A shared node is `UNCOVERED` in v1 (OD-A1) |
| C — cluster control plane | Admission webhooks, CRDs/operators, RBAC, PriorityClass/preemption, DNS, CNI, API-server load | The closure contains a cluster-scoped or control-plane kind | The closure contains a cluster-scoped or control-plane kind, or an object that registers a webhook or controller | v1: **never covered.** Any closure touching these kinds is `INAPPLICABLE` for exclusion |
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
3. Channel K: the journal is complete for the window, and every closure kind has a declared complete reference table.
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

## 8. Amendment rule

If implementation finds that the code or data semantics conflict with a clause, it stops with `CONTRACT_AMENDMENT_REQUIRED`. The report names the clause, the contrary evidence, the minimal proposed amendment and the gate impact. Only an owner-approved, recorded amendment may change `m21.v1`.

## 9. Amendment record

| Date | Amendment | Source |
|---|---|---|
| 2026-09-27 | Amendment 1 (owner `CONTRACT_AMENDMENT_REQUIRED`). Channel applicability separated from coverage (§4.2a). Exclusion rule restated over applicable channels with an empty-set guard (§4.3). OD-A1 replaced by structural shared-node applicability. Invariants I10–I13 added. Noisy-neighbor, vacuous-truth and applicability-is-not-evidence negative controls added. OD-A2 excludes `Secret`. OD-A4 is bounded to strict, windowed, identity-resolved, coverage-qualified neighbors. The other 10 decisions approved. D1 gate set to exact epistemic-digest equality. | Owner review of `m21.v0` |
| 2026-09-27 | Amendment 2 (owner `CONTRACT_AMENDMENT_REQUIRED`). I12 changed from structural-only to evidence-independent structural or declarative applicability, consistent with I10, OD-A3 and OD-B1. D2 support consequences are included in the epistemic digest independently of the aggregate state (digest invariant), and a D2 digest-visibility negative control is added. The D1 migration control is restated unchanged. Decision matrix D reads "existing epistemic state remains unchanged". | Owner review of amendment 1 |
