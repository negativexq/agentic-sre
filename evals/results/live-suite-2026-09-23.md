# Live scenario suite — regression run

**Date:** 2026-09-23
**Code:** `9e8add2` (current HEAD — the full UI/productization track, auth guard,
report/email pipeline, migrations `0012`–`0014`, and the live-runner bearer fix).
**Cluster:** single-node `kind` (`agentic-sre`); deterministic demo workload,
every alert metric-based. **Command:** `make live-bench`.

**Why this run:** the prior recorded result (`637e20c`, 24/25) predated the
UI/productization track. Control-plane, storage, report, SSE, auth and migration
code all changed since; `packages/rca/` did not. This clean-window run
re-anchors the live-path result to current HEAD and confirms the track did not
regress it. Model calls are verified against the cluster, not assumed: the 96
diagnoses stored during this run are all `mode=deterministic` with
`sum(model_calls) = 0`.

```text
Code: 9e8add2
Cluster: kind / single-node
Scenarios: 25
Model calls: 0
Root-cause actor: 16/16
Abstention: 9/9
Overall expected behaviour: 25/25

RESOLVED: 0
AMBIGUOUS: 14
INSUFFICIENT_EVIDENCE: 11

NO_INCIDENT: 0
NO_DIAGNOSIS: 0
ERROR: 0

Median alert time: 25.2s
p95 alert time: 65.5s
```

## Gate

| Gate | Pass | Value |
|---|:---:|---|
| Harness: 0 NO_INCIDENT / NO_DIAGNOSIS / ERROR | ✅ | 0 failures |
| Safety: 0 confident fabrication | ✅ | 0/9 |
| Root actor >= 15/16 | ✅ | 16/16 |
| Overall >= 24/25 | ✅ | 25/25 |
| Model calls: 0 | ✅ | 0 |

## Delta vs the prior run (`637e20c`, 2026-09-22: 24/25)

Scenario-by-scenario diff against
[`live-suite-2026-09-22.json`](live-suite-2026-09-22.json):

- **One outcome changed:** `rollout_restart_disruption` went
  `WRONG_ACTOR → CORRECT` (this is the single scenario that lifts 24/25 → 25/25;
  root-cause actors are now 16/16).
- **Everything else is stable.** The resolution distribution is **identical** —
  14 `AMBIGUOUS` / 11 `INSUFFICIENT_EVIDENCE` / 0 `RESOLVED` in both runs — and
  all abstentions held (9/9, 0 fabrications). The other per-row differences are
  only pod/replicaset **names** (redeploy scheduled new pods, e.g.
  `ReplicaSet/order-service-555b8dc77` → `Pod/order-service-587dc4df7-...`), the
  same logical actor.

**Honest reading of the +1.** `packages/rca/` is unchanged between the two runs,
so the flip is **not** a code improvement — `rollout_restart_disruption` is a
borderline case whose live outcome varies run-to-run (pod scheduling and alert
timing). The reproducible, code-attributable claim is that the clean-window
behavior is **preserved**: same resolution distribution, abstentions intact, 0
fabrications, 0 model calls. Overall is 25/25 this run and has not dropped below
the 24/25 baseline.

## Per-scenario

| Scenario | Expected | Outcome | Resolution | Actual | Alert s |
|---|---|---|---|---|---:|
| `payment_error_spike` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/payment-service-667fff77b9-4xdkp` | 25.2 |
| `order_error_spike` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-587dc4df7-h9hjs` | 25.2 |
| `payment_dependency_latency` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-587dc4df7-h9hjs` | 30.3 |
| `order_latency_spike` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-587dc4df7-h9hjs` | 25.1 |
| `payment_db_pool_pressure` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/payment-service-667fff77b9-4xdkp` | 50.3 |
| `order_db_query_latency` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-587dc4df7-h9hjs` | 25.2 |
| `cross_service_payment_failure` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-587dc4df7-h9hjs` | 25.2 |
| `cross_service_db_pressure` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-587dc4df7-h9hjs` | 30.4 |
| `payment_db_query_latency` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/payment-service-6bc5657cd7-9nblj` | 25.1 |
| `order_worker_lag` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/order-worker` | 35.2 |
| `order_worker_failure` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/order-worker` | 85.5 |
| `payment_config_change` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/payment-service` | 20.1 |
| `order_config_latency` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/order-service` | 20.1 |
| `payment_config_cross_service_impact` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/payment-service` | 40.2 |
| `dependency_endpoint_misconfig` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/order-service` | 35.2 |
| `configmap_dependency_change` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/ConfigMap/workload-config` | 35.3 |
| `image_regression` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Pod/payment-service-86bdc56fbd-6rbkr` | 25.2 |
| `dependency_scaled_to_zero` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/payment-service` | 25.2 |
| `rollout_restart_disruption` | root-cause | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Deployment/payment-service` | 25.3 |
| `network_policy_isolation` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/NetworkPolicy/deny-order-egress` | 45.2 |
| `memory_limit_oom` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Pod/payment-service-85b5dff97c-kw6j4` | 25.2 |
| `cpu_limit_throttle` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/payment-service` | 30.1 |
| `service_selector_drift` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Service/payment-service` | 20.1 |
| `worker_deployment_deleted` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/order-worker` | 65.5 |
| `payment_pod_crash` | root-cause | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/payment-service-675bdc99d9-c5cqm` | 0.0 |
