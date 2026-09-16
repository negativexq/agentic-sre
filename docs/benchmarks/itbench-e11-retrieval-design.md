# E11 observed-entity and retrieval design

E11 separates the complete observed catalog from the bounded model shortlist:

```text
structured snapshot evidence
        ↓
ObservedEntityCatalog (stable C### handles)
        ↓
evidence-derived ranking
        ↓
diverse active shortlist
```

The catalog accepts direct Kubernetes objects and involved-object event
identities, explicit alert labels, and structured resource identities from
metrics, logs, and traces. Telemetry identities remain `ServiceIdentity` or
`TelemetryResourceIdentity` unless a direct object or explicit topology edge
proves a Kubernetes mapping. Names and substrings are never used to invent an
identity. Each record retains source categories, provenance, evidence refs,
aliases, observation times, identity quality, and directed relationships.

Ranking uses bounded, inspectable channels: diagnostic alert linkage,
classified failure events, measurable metric change, structured log/trace
errors, temporal alignment, and directed owner/configuration/selector,
scaling, disruption, call, and namespace relationships. Repeated rows are
capped per channel. Generic control-plane alerts, normal informational events,
object-kind priors, name-token boosts, topology centrality, and subsystem
blacklists are not causal evidence.

Explicit relations carry evidence to defensible upstream candidates: workload
signals can reach referenced configuration, owner/controller, scaling policy,
selected policy, disruption, or namespace identities. Signal propagation is
bounded and recorded as a causal relation, so a derived score remains
auditable rather than an opaque confidence value. The shortlist applies a
small family cap only after scoring and preserves all runtime handles.

Candidate discovery may add a new structured identity after investigation.
Handles are monotonic and never renamed; ranking revisions record the trigger,
previous order, and new order. E11 control memory additionally records
`SUPPORTS`, `CONTRADICTS`, or `INCONCLUSIVE` assessments with evidence refs.
Submission requires the current hypothesis to be `SUPPORTED` and free of an
unresolved contradiction; otherwise `STOP` remains legal.

The catalog and ranking are generated without ground truth and frozen before
post-hoc evaluation. The offline qualification reports the R0–R5 ablation and
the immutable E7 baseline. `E11 LIVE BENCHMARK: NOT RUN`.
