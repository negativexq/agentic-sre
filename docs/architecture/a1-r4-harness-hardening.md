# A1-R4 benchmark harness hardening

This note records the zero-model reliability controls used before the next A1
live execution. It describes benchmark infrastructure only; it is not part of
the investigator prompt and contains no evaluator answer data.

## Previous lifecycle and failure modes

The earlier runner performed environment preparation, fixture mutation,
stimulus, alert/incident polling, investigation, cleanup, and final aggregate
serialization. The important gaps were that preparation was not represented as
an explicit oracle phase, workload delivery was partly best-effort, trigger
activation was inferred from alert delivery, and completed results lived in RAM
until the end of the run. A later infrastructure failure could therefore hide
already-consumed work.

The hardened lifecycle is:

```text
prepare environment -> verify baseline -> inject fault -> verify fault
-> run workload -> verify workload -> verify trigger -> wait for alert
-> wait for incident -> run agent -> persist result -> grade
-> cleanup fault -> verify recovery -> reconcile baseline -> complete
```

Every phase is appended to an fsync'd JSONL ledger. A missing end record means
the phase was incomplete. A torn final JSONL line is ignored only at the end of
the file.

## State policy

| Subsystem | Per-scenario action | Reason |
| --- | --- | --- |
| Control-plane incidents/alerts | Guarded local benchmark reset and empty-state check | Canonical fingerprints must correlate to a fresh incident. |
| Prometheus and Alertmanager | Restart only the local benchmark deployments, then readiness-check | Their stores are ephemeral and sliding-window/active-alert state can contaminate a trial. |
| OTel collector | Readiness and stable deployment check; no blind reset | Its output is consumed through the restarted metric path; it has no benchmark-specific reset requirement. |
| Workload fault environment | Remove tracked fault variables and verify clean state | Faults must not leak across trials. |
| Workload deployments | Health and rollout reconciliation; replace unhealthy payment runtime when required | A previous crash must not change the next fixture's starting state. |
| Kafka counters/history and consumer offsets | Preserve; capture counter baselines and use bounded deltas/windows | Blind topic deletion or offset mutation is unnecessary for the current proxy semantics. |
| Redis worker state | Preserve; current fixtures are idempotent and bounded, with no evidence that old keys affect the alert windows | Reset only if a future fixture proves state contamination. |
| Loki and Tempo | Preserve; every query is incident-window bounded | Restarting them would add cost without removing a demonstrated contamination source. |
| Change records | Preserve as operational history; query through the bounded incident/change window | Change evidence must remain available without unbounded historical leakage. |
| Provider ledger and local result files | Preserve history; use a distinct execution ledger and run directory | Transmitted calls and checkpoints are never silently refunded or overwritten. |

The selected reset strategy is therefore the smallest demonstrated one:
control-plane reset, Prometheus/Alertmanager ephemeral-store reset, bounded
workload-runtime reconciliation, and a baseline oracle. It is not a full
cluster reset. The Kubernetes mutation path is guarded to the exact local kind
context and is not registered as an investigation tool.

## Oracles

The environment oracle checks workload health, Prometheus/Alertmanager/OTel
readiness, quiet active alerts, empty canonical incident state, clean tracked
fault variables, stable worker rollout, and a valid Prometheus `up` query.

Fault and workload oracles are separate: a deployment patch becoming accepted
does not prove its new pod is active, and an attempted HTTP request does not
prove delivery. HTTP error responses from an intentionally faulted service are
counted as delivered workload responses; transport failures are counted as
failed delivery. V020-007 additionally proves successful order count and
produced Kafka counter delta.

The trigger oracle is independent from alert delivery. For V020-007 it records
lag peak, threshold, first/last threshold observations, time above threshold,
Prometheus pending/firing, and Alertmanager firing. The frozen
`OrderWorkerLagHigh` expression, `> 10` threshold, and 15-second `for` clause
are unchanged.

## Durable results

Live benchmark runs use an execution-scoped checkpoint store. Each validated
scenario checkpoint is JSON-serialized, reloaded through the strict artifact
boundary, atomically replaced from a same-directory temporary file, fsync'd,
and referenced by SHA-256. A partial-run record is written when an
infrastructure invalidation occurs. Final aggregation reads the ordered
checkpoints, not only an in-memory list.

The phase ledger and checkpoint store are deliberately outside investigation
authority. They do not add provider tools, shell access, Kubernetes writes, or
remediation actions.

## Change and Kafka notes

Change records retain their operational history. Benchmark evidence uses the
incident-derived time window and the existing bounded change lookback, so a
prior benchmark change is not automatically treated as current causal
evidence. A future implementation may add scenario-scoped change revisions;
the current A1 model-facing semantics are unchanged.

The current worker-lag signal is the bounded produced-minus-consumed increase
proxy. A true consumer-group offset lag implementation is documented as a
post-A1 option in [the lag ADR](post-a1-kafka-lag-adr.md) and is not used for
this experiment.
