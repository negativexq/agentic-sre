# M21 Causal Semantics Contract

Contract version: `m21.v0`
Status: **DRAFT — awaiting owner freeze.** No M21 production rule may be implemented before this document is frozen.
Baseline: `main` @ `d0caf5d0e3512930c1a8d4e0803e777a64fb2f06` (RCA code identical to the M20.1 audit)
Scope: this contract defines the evidence that authorizes the RCA engine to treat a hypothesis as root-capable, root-supported or root-ineligible. It covers four topics: A unrelated initiated changes, B telemetry and control-plane actors, C asynchronous propagation, and D positive root support. It changes no code, test or threshold.

`m16.v1` (including amendments A1 `m16.ended-manifestation-episode.v1` and A2 `m16.resource-pressure.v1`) and `m18a.v1` remain binding. Where this contract is silent, they govern. This contract adds nothing to them and relaxes none of their clauses.

## 1. The question this contract answers

> By which positive or negative causal evidence may we treat a hypothesis as **root-capable**, **root-supported**, or **root-ineligible**?

| Term | Meaning | Existing mechanism |
|---|---|---|
| root-capable | The hypothesis may compete for root cause. This is the default for every generated hypothesis. | `RootCauseEligibilityState.ELIGIBLE` / `UNDETERMINED` |
| root-supported | A named deterministic rule has fired on positive evidence that the actor initiated the incident. | Today only the implicit `SUPPORTED` predicate (§6.2) |
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
- **I2 — Asymmetric evidence strength.** Weak evidence may *keep* an alternative in competition. Only strong, complete, positively covered evidence may *remove* one. For example, an aggregate destination-level async relation (§7) counts as an open channel for A, but never establishes async propagated-effect ineligibility.
- **I3 — Truth-blind and name-blind.** No rule may depend on scenario IDs, ground truth, or specific object, namespace, image or label *values* chosen to match a benchmark. Actor classification comes only from typed evidence or from operator-declared, versioned configuration (§5.2.1). Any such configuration is part of the RCA config digest and is identical across every incident it is applied to.
- **I4 — Three-valued rule outcomes, no scores.** Every rule evaluates to `FIRED`, `NOT_FIRED` or `INAPPLICABLE`, and records every precondition result. No rule produces a weight, probability or support score, and ranking never feeds a rule.
- **I5 — Replayable inputs only.** Every rule reads only manifest members and provider-tape results of the run (M19). The M20.1b base replay must reproduce every new consequence digest. Coverage is computed from the same persisted inputs, never from a live re-query.
- **I6 — Authority boundary unchanged.** Only deterministic RCA consequence code may change eligibility, support or epistemic state. The planner, investigation tools, normalizers and any LLM have no authority (m16 §1).
- **I7 — Frozen rules untouched.** A1, A2, temporal contradiction and `ROOT_CAUSE_INELIGIBLE_PROPAGATED_EFFECT` keep their IDs, versions and meanings.
- **I8 — Audit.** Every consequence carries `rule_id`, `rule_version`, `consequence`, exact `evidence_ids` and source `observation_ids`, `targets`, `time_basis`, a `coverage` record (§4.3), and per-precondition results (m16 §11). Structured fields are authoritative; any explanatory text is secondary.
- **I9 — Exclusion is incident-scoped.** Root-ineligibility says only that the actor did not initiate *this* incident under the named rule. It never denies the observed change or failure.

## 4. Shared framework: influence channels and coverage

Topics A and B (and the async part of C) share one idea. An actor can only have caused a symptom through some **influence channel**. The actor can be excluded only when every channel it could use is **positively covered** and shows no path to the symptom.

### 4.1 Channel catalog (`m21.channel-catalog.v1`, proposed)

| Channel | Meaning | Path evidence (keeps the actor alive) | Coverage evidence (needed to claim "no path") |
|---|---|---|---|
| K — Kubernetes reference | Owner/selector/routing/config/secret/volume/env/service-account/network-policy/quota/limit-range/PDB/HPA/scheduling references, including namespaceSelector and cross-namespace references | Any reference chain from the change closure (§4.2) to a symptom-side entity | Object journal complete for every namespace and cluster-scoped kind in the window, **and** the relation extractor declares complete reference coverage for every kind in the closure (a versioned per-kind table) |
| R — synchronous runtime | Traced CLIENT→SERVER calls | Any traced call edge between closure services and symptom-side services, in either direction, in the window | Every Pod in the closure is instrumented (it emitted spans in the window) and trace reads for the window completed without error or truncation |
| M — asynchronous messaging | PRODUCER → destination → CONSUMER | Any message-level or destination-level relation (§7) between the closure and the symptom side | Messaging instrumentation present on the closure's producers and consumers, and complete trace reads |
| N — shared node resources | CPU, memory, disk, network or PID contention on a shared node | A closure Pod and a symptom-side Pod shared a node in the window | Both node placements known for the whole window, **and** either no shared node, or each shared node's resources positively measured normal over the window (a future rule; see OD-A1) |
| C — cluster control plane | Admission webhooks, CRDs/operators, RBAC, PriorityClass/preemption, DNS, CNI, API-server load | The closure contains a cluster-scoped or control-plane kind, or an object that registers a webhook or controller | v1: **never covered.** Any closure touching these kinds is `INAPPLICABLE` for exclusion |
| O — observation path | The actor transports, stores or computes telemetry the symptom was measured from | The alert or symptom signal's provenance passes through the actor | The alert's signal provenance is known and positively does not pass through the actor |

Channel O matters because a telemetry actor can create or hide a symptom without touching the application (§5).

### 4.2 Change closure

The **closure** of an initiated change on object X is X plus every object X owns, contains, creates or configures within the window, via channel K. Examples: the Namespace plus the objects inside it; the Job plus its Pods; the ConfigMap plus the Pods that mount it. Channel evaluation applies to the whole closure. If the closure cannot be computed positively (unknown kinds, CRDs, missing journal data), the rule is `INAPPLICABLE`.

### 4.3 Coverage record

Each channel evaluation produces a record containing:
- the channel;
- `state ∈ {PATH, NO_PATH_COVERED, UNCOVERED, UNKNOWN}`;
- the window;
- the basis evidence and observation ids;
- the reason for any gap.

Only `NO_PATH_COVERED` on **every** catalog channel lets an exclusion rule fire. Any `PATH` makes the rule `NOT_FIRED`. Any `UNCOVERED` or `UNKNOWN` makes it `INAPPLICABLE`, and the alternative stays in competition.

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

**PROPOSED SEMANTIC.** An initiated change is **root-ineligible for this incident** only when, for its full closure, every catalog channel (§4.1) evaluates to `NO_PATH_COVERED` over the full window (§4.4). The consequence is `ROOT_INELIGIBILITY`, not `CONTRADICTED`: the change happened; it is just not this incident's initiator. The rule never supports another hypothesis and never selects an actor.

**REQUIRED EVIDENCE.**
- The exact change Finding with its causal time.
- The journal versions of every closure object.
- The symptom-side entity set, derived from the alert (the existing `symptom_entities`) plus its K-closure.
- The per-channel coverage records with evidence ids.
- The trace read results used for channels R and M.
- Pod placement records for channel N.

**COVERAGE PRECONDITIONS.**
1. The closure is computable and contains only kinds in the v1 closed kind set. OD-A2 proposes Namespace, ConfigMap, Secret, Service, ServiceAccount, Job and Pod.
2. Channel K: the journal is complete for the window, and every closure kind has a declared complete reference table.
3. Channels R and M: every closure Pod is instrumented, and the trace reads for the window completed.
4. Channel N: placement is known for the whole window, and there is no shared node with symptom-side Pods. In v1, a shared node counts as `UNCOVERED` (OD-A1).
5. Channel C: the closure contains no control-plane or cluster-scoped kind.
6. Channel O: the alert's signal provenance is known and excludes the closure.
7. The change time is certain against the window.

**MISSING-EVIDENCE BEHAVIOR.** If any precondition is unknown or partial, the rule is `INAPPLICABLE`, the hypothesis keeps its current state and eligibility, and the diagnosis stays `AMBIGUOUS`. Two cases get specific treatment:
- A closure with running uninstrumented Pods cannot close channel R.
- A Pod-bearing closure on a shared node cannot close channel N in v1.

The five `TRAFFIC_INCREASE` blockers are observations, not operator changes; their treatment is open (OD-A3).

**FALSE-RESOLVED FAILURE MODE.** The dangerous case is a change that really initiated the incident, excluded because its channel was not modeled while the coverage records claimed completeness. Required negative controls for the M21 gate:
- (a) A new-namespace Job that saturates a node shared with the symptom service must not be excluded (channel N).
- (b) A ConfigMap consumed through an unmodeled reference must be `INAPPLICABLE`, not excluded (channel K completeness).
- (c) A change reaching the symptom only through an uninstrumented service must be `INAPPLICABLE` (channel R).
- (d) A change to a webhook or CRD must be `INAPPLICABLE` (channel C).

**VERSIONED RULE CANDIDATE.** `m21.unrelated-change.v1` (proposed):
- reason code `NO_INFLUENCE_CHANNEL_UNDER_COVERAGE` (proposed new code);
- consequence `ROOT_INELIGIBILITY`;
- mechanism `INFLUENCE_CHANNEL`;
- targets: actor plus closure;
- audit: one coverage record per channel.

**OPEN OWNER DECISIONS.**
- **OD-A1** — Channel N in v1: is it always `UNCOVERED` for Pod-bearing closures on shared nodes, or does a future node-resource normality rule close it? Recommendation: always `UNCOVERED` in v1. Node normality needs its own contract, like A2.
- **OD-A2** — The v1 closed kind set. Recommendation: Namespace, ConfigMap, Secret, Service, ServiceAccount, Job, Pod. Everything else is `INAPPLICABLE`.
- **OD-A3** — Is `TRAFFIC_INCREASE` an initiated change for this rule? Its closure is the Service's callers and callees, and its channels are R, M and N. Recommendation: include it, using the same channel test, with channel O mandatory (scrape traffic is observation-path traffic).
- **OD-A4** — Is the symptom side the alert's entities plus their K-closure only, or also their traced callers and callees? Recommendation: include traced neighbors. A larger symptom side makes exclusion harder, which is the safe direction.

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

Domain does not exclude anything by itself. It selects which channels an actor has. For an actor whose domain differs from the symptom domain, root-ineligibility follows the §4 channel test, with channel O mandatory:
- A `TELEMETRY` actor in an `APPLICATION` incident is root-ineligible only if:
  - channel O is covered: the alert's provenance positively excludes the actor;
  - channels K, R, M and N are `NO_PATH_COVERED` (for example, no application span shows a synchronous export failure in its request path, and there is no shared node);
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

**OPEN OWNER DECISIONS.**
- **OD-B1** — Is the domain source operator-declared configuration only (recommended), or may typed runtime evidence infer it? An example of the latter: an actor that only receives OTLP export and serves no traced RPC.
- **OD-B2** — May the ITBench evaluation configuration ship **one per-application** domain declaration for otel-demo? It would be frozen before the test split and identical for every scenario. Recommendation: yes, declared once and recorded in the eval config digest. Scenario-specific declarations are forbidden.
- **OD-B3** — Where does alert signal provenance come from in v1? Options: alert-rule metadata, or a declared mapping from alert name to pipeline. Recommendation: a declared mapping. Where it is missing, channel O is `UNKNOWN`.

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

**OPEN OWNER DECISIONS.**
- **OD-C1** — Should async be a new rule with a new code (recommended), or v2 of `m16.root-eligibility-propagated-effect`?
- **OD-C2** — Confirm that MSG-3 may never establish ineligibility (recommended: never).
- **OD-C3** — Should the broker, as a root candidate from producer send failures, be in M21 v1 or deferred? Recommendation: defer to a D rule (§5.4 D2) rather than an exclusion.

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
  - Its introduction must leave every diagnosis digest-equal apart from the audit fields, and M20.1b replay verifies this.
- **D2 `m21.support.runtime-failure-origin.v1`** (new). The actor is the **origin** of non-success in traced requests. All of these must hold:
  - It is the callee in ≥ 1 `REMOTE_NON_SUCCESS_PROPAGATED` pair.
  - Within the same traces, all of the actor's own traced outgoing calls succeeded, or there were none and the actor is instrumented.
  - The first non-success is after the last healthy observation of the same operation. This is the onset alignment. The healthy baseline is required: an actor that was always failing is `INAPPLICABLE`.
  - The binding is exactly verified.
  - Coverage: the actor emitted spans across the window, and trace reads completed.
  - Consequence: `ROOT_SUPPORT` of kind `FAILURE_ORIGIN`. This establishes *failure origin within the actor's boundary* (its code, config or untraced dependencies). It is support, not proof of initiation. It enables dominance only under OD-D1.
- **D3 (deferred).** Explained manifestation, or configuration-dependency influence. A supported change on X that X's consumer Y reads (for example flag configuration → flagd → payment) would *explain* Y's origin evidence. That would make Y root-ineligible as `EXPLAINED_BY_SUPPORTED_ROOT`. This is an exclusion by positive explanation. It needs its own contract (OD-D2) and is **not** authorized here.

**REQUIRED EVIDENCE.**
- D1: the change Finding, the journal versions and the path hops.
- D2: the exact span pairs (evidence ids), the actor's outgoing pairs in the same traces, the healthy-baseline observation ids, the binding verification ids and the trace coverage record.

**COVERAGE PRECONDITIONS.**
- D2 requires the actor to be instrumented across the whole window and the trace reads to be complete.
- An outgoing call to an uninstrumented dependency makes D2 `INAPPLICABLE`: the failure may come from beyond the traced boundary.

**MISSING-EVIDENCE BEHAVIOR.** A rule that cannot be evaluated is `INAPPLICABLE` and records why. `NOT_FIRED` is recorded when all inputs are covered and the predicate is false. Neither changes eligibility. A hypothesis with no fired rule remains `UNRESOLVED` exactly as today.

**FALSE-RESOLVED FAILURE MODE.**
- A D2-supported actor is resolved while the real initiating change elsewhere was excluded by a weak rule. D2 cannot resolve by itself, so any false resolution must come from an A/B/C exclusion. For that reason, the M21 gate attributes every `RESOLVED` to the exclusions that enabled it.
- A second mode: D2 support feeding dominance could let a failure-origin actor dominate the actual changed actor.

Required negative controls:
- (a) A flag-config change causing payment errors must not resolve to payment.
- (b) An always-failing actor gives D2 `INAPPLICABLE`.
- (c) An actor calling an uninstrumented dependency gives D2 `INAPPLICABLE`.

**VERSIONED RULE CANDIDATE.** `m21.support.change-onset-path.v1` and `m21.support.runtime-failure-origin.v1`, both with consequence `ROOT_SUPPORT` (a new consequence kind). The support record is persisted in the hypothesis audit and included in the epistemic digest.

**OPEN OWNER DECISIONS.**
- **OD-D1** — May D2 support participate in dominance? Recommendation: no in M21 v1. Dominance stays change-shape-based, so D2 cannot out-rank an initiating change.
- **OD-D2** — Is D3 (explained manifestation) in M21 scope, with its own contract, or deferred to M22+? Recommendation: defer. It is the most promising route for C5, but it is an exclusion by explanation and carries its own false-resolved risk.
- **OD-D3** — Should D1 be a pure audit refactor with no behavior change, gated by digest equality apart from audit fields? Recommendation: yes. It is the first M21 implementation step.

## 6. Decision matrix

| Topic | Automatic exclusion? | Positive support? | Missing-evidence behavior | M21 implementation |
|---|---|---|---|---|
| A — unrelated change | Conditional: `ROOT_INELIGIBILITY` only when every catalog channel is `NO_PATH_COVERED` over the full window | No | Neutral: `INAPPLICABLE`, stays in competition | Yes: `m21.unrelated-change.v1` |
| B — telemetry, control-plane and infrastructure actors | Role-aware: only a declared cross-domain actor with channel O and all channels covered. Control-plane and infrastructure actors never in v1. | Conditional: via D rules when the actor's domain equals the symptom domain | Neutral | Yes: `m21.incident-causal-scope.v1` plus the domain config |
| C — async propagation | Async propagated-effect eligibility with MSG-1/MSG-2 only; MSG-3 never | May follow later (the broker as origin), not v1 | Neutral | Yes: `m21.async-propagated-effect.v1` plus normalizer preservation (M20.5) |
| D — positive root support | No | Yes: versioned rules D1, D2; FIRED / NOT_FIRED / INAPPLICABLE; no scores | Neutral; state stays `UNRESOLVED` | Yes: D1 (audit refactor) then D2; D3 deferred |

## 7. Implementation order and gate (after freeze, after M20)

Order: the M20 live-parity tasks first (owner-frozen order). The M21 steps then follow:
1. D1 audit refactor, gated by digest equality apart from the audit fields.
2. The coverage record and channel catalog (§4).
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
