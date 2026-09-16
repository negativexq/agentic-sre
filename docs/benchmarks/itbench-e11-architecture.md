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

The fake-provider canary is a control-plane test, not an RCA-quality result. `E11 LIVE BENCHMARK: NOT RUN`.
