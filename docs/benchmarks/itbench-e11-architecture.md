# E11 investigation architecture

```text
Observable runtime evidence
        ↓
ObservedEntityCatalog (stable C### handles + provenance)
        ↓
evidence-derived ranking and bounded active shortlist
        ↓
OBSERVE
        ↓
HYPOTHESIZE
        ↓
discriminating VERIFY
        ↓
SUPPORTS / CONTRADICTS / INCONCLUSIVE
        ↓
dynamic discovery and reranking
        ↓
REVISE → SUPPORTED candidate → SUBMIT
                         ↘ insufficient support → STOP
```

The catalog accepts structured Kubernetes objects/events, alert labels, and bounded structured metrics, logs, and traces. A telemetry service identity remains a `ServiceIdentity`/`TelemetryResourceIdentity` unless observable metadata proves a Kubernetes mapping. Every candidate records source categories, evidence references, aliases, identity quality, and directed relationships.

Ranking uses alert linkage, classified failure events, measurable metric change, structured log/trace errors, temporal alignment, and explicit directed topology. Repeated observations are capped per channel and generic control-plane alerts are ignored as causal seeds. No score is derived from evaluator truth, object-kind priors, subsystem name bans, or model prose.

E11 uses a compact bounded context packet and a first-class `OBSERVE` phase. Candidate handles are allocated once and remain stable when new identities are discovered. Candidate status and evidence polarity are typed; submission requires a currently hypothesized candidate with supporting evidence and no contradiction. Missing change history remains unavailable rather than being inferred.

Before retrieval, the snapshot evidence layer is qualified for timestamp-order invariance, exact structured telemetry identity matching, container/init-container resource extraction, bounded `LOG_ANALYSIS`, and owner/label-based replica peer resolution. `COMPARE_REPLICAS` and change analysis are capability-gated by actual observable evidence.

The complete observed entity catalog is built from Kubernetes objects/events, diagnostic alert labels, structured metric/log/trace identities, and directed topology. Ranking uses inspectable alert, failure-event, metric-anomaly, log, trace-origin, temporal, and directed-relationship signals; object kind, graph degree, and name tokens are not causal priors. The active shortlist is bounded and diversity-aware, while later evidence may allocate stable handles and trigger a ranking revision.

The verification surface records operation-to-target scopes for the current hypothesis and bounded visible alternatives, so comparison does not require an irreversible `REVISE` first. Empty results remain non-supporting evidence, and `SUBMIT` requires a supported candidate with valid supporting evidence and no contradiction.

The fake-provider canary is a control-plane test, not an RCA-quality result.
`E11InvestigationRuntime` is the provider-injected production loop used by
that canary and by the future OpenAI adapter; there is no separate fake FSM.
Its persisted trajectory includes context metadata, semantic evidence,
assessment history, ranking revisions, event state, usage, and termination
reason. `E11 LIVE BENCHMARK: NOT RUN`.
