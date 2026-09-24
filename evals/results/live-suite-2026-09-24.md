# Live scenario suite — regression run

**Date:** 2026-09-24
**Code:** `da6f0e6`
**Cluster:** single-node `kind` (`agentic-sre`); deterministic demo workload,
every alert metric-based. **Command:** `make live-bench`.

## Current-HEAD re-anchor provenance

This complete run evaluated code commit
`da6f0e671775e3ddba9374d3b2fd46e678478a56` on branch `main`, with
`origin/main` at the same commit before the run. The working tree was clean.
The fresh single-node Kind cluster was `agentic-sre` (context
`kind-agentic-sre`, kind v0.33.0, Kubernetes client v1.36.1). Workloads and
observability deployments were healthy before the run and restored afterward.

The suite ran from `2026-09-24T13:19:06Z` through
`2026-09-24T13:46:56Z`. The raw runner JSON is preserved unchanged in this
artifact; SHA-256:
`9721454bff60be89a0cdf4e12a2d2146d46feaa3aa99881954e7e1d59a6ee229`.

The historical `2026-09-23` result remains evidence for commit `9e8add2` only.
This fresh run is the current-HEAD evidence; it does not rewrite or replace the
historical artifact.

## Abstention safety and model-call verification

All nine abstention cases were `CORRECT`, `INSUFFICIENT_EVIDENCE`, and
`UNVERIFIED`. None was `RESOLVED`; none was graded `FABRICATED`. Their
non-empty `actual` fields are diagnostic Pod observations, not causal answers:
the live grader treats an abstention as fabricated only when the diagnosis is
`RESOLVED`.

For each of the 25 incident IDs in the raw results, the persisted diagnosis
endpoint was queried after the run. Every record reported `mode=deterministic`
and `model_calls=0`; the mechanically summed total is 0. The per-incident
verification records are retained in the ignored local re-anchor directory.

## Comparison with the historical `9e8add2` run

The scenario ID set and all 25 expectations match. Outcome, resolution,
confidence, and expected-Finding support status are unchanged for every
scenario. Root actor grading remains 16/16 and abstention grading remains 9/9.
There are no outcome or resolution deltas and no unexplained behavioral
regressions.

The raw `actual` entity string changed for 12 scenarios because the fresh
cluster generated new Pod/ReplicaSet identities. Existing live grader
semantics accept a Pod owned by the expected Deployment; these names still
identify the same logical workload. The other result-field change is one
additional `FAILURE_EVENT` in `payment_config_cross_service_impact`; its
`SPEC_CHANGE` evidence, expected Deployment actor, `CORRECT` outcome, and
`AMBIGUOUS` resolution are unchanged. This is classified as live evidence
variance. Alert arrival times were freshly measured and vary with live timing.

Two root-cause cases, `image_regression` and `memory_limit_oom`, still have
`supported_by_expected_kind=false` despite the correct actor. This flag was
also false in the historical result; it is not a new regression and remains a
known “right actor, unsupported expected Finding kind” caveat.

| Scenario | Old outcome / actor / resolution | Current outcome / actor / resolution | Classification |
|---|---|---|---|
| `payment_error_spike` | CORRECT / `Pod/payment-service-667fff77b9-4xdkp` / INSUFFICIENT_EVIDENCE | CORRECT / `Pod/payment-service-659c86bc64-jt2km` / INSUFFICIENT_EVIDENCE | HARNESS_VARIANCE — new Pod identity; abstention unchanged |
| `order_error_spike` | CORRECT / `Pod/order-service-587dc4df7-h9hjs` / INSUFFICIENT_EVIDENCE | CORRECT / `Pod/order-service-568775468b-6mswv` / INSUFFICIENT_EVIDENCE | HARNESS_VARIANCE — new Pod identity; abstention unchanged |
| `payment_dependency_latency` | CORRECT / `Pod/order-service-587dc4df7-h9hjs` / INSUFFICIENT_EVIDENCE | CORRECT / `Pod/order-service-568775468b-6mswv` / INSUFFICIENT_EVIDENCE | HARNESS_VARIANCE — new Pod identity; abstention unchanged |
| `order_latency_spike` | CORRECT / `Pod/order-service-587dc4df7-h9hjs` / INSUFFICIENT_EVIDENCE | CORRECT / `Pod/order-service-568775468b-6mswv` / INSUFFICIENT_EVIDENCE | HARNESS_VARIANCE — new Pod identity; abstention unchanged |
| `payment_db_pool_pressure` | CORRECT / `Pod/payment-service-667fff77b9-4xdkp` / INSUFFICIENT_EVIDENCE | CORRECT / `Pod/payment-service-659c86bc64-jt2km` / INSUFFICIENT_EVIDENCE | HARNESS_VARIANCE — new Pod identity; abstention unchanged |
| `order_db_query_latency` | CORRECT / `Pod/order-service-587dc4df7-h9hjs` / INSUFFICIENT_EVIDENCE | CORRECT / `Pod/order-service-568775468b-6mswv` / INSUFFICIENT_EVIDENCE | HARNESS_VARIANCE — new Pod identity; abstention unchanged |
| `cross_service_payment_failure` | CORRECT / `Pod/order-service-587dc4df7-h9hjs` / INSUFFICIENT_EVIDENCE | CORRECT / `Pod/order-service-568775468b-6mswv` / INSUFFICIENT_EVIDENCE | HARNESS_VARIANCE — new Pod identity; abstention unchanged |
| `cross_service_db_pressure` | CORRECT / `Pod/order-service-587dc4df7-h9hjs` / INSUFFICIENT_EVIDENCE | CORRECT / `Pod/order-service-568775468b-6mswv` / INSUFFICIENT_EVIDENCE | HARNESS_VARIANCE — new Pod identity; abstention unchanged |
| `payment_db_query_latency` | CORRECT / `Pod/payment-service-6bc5657cd7-9nblj` / INSUFFICIENT_EVIDENCE | CORRECT / `Pod/payment-service-659c86bc64-jt2km` / INSUFFICIENT_EVIDENCE | HARNESS_VARIANCE — new Pod identity; abstention unchanged |
| `image_regression` | CORRECT / `Pod/payment-service-86bdc56fbd-6rbkr` / AMBIGUOUS | CORRECT / `Pod/payment-service-565dd98c-rhmtg` / AMBIGUOUS | HARNESS_VARIANCE — Pod remains owned by expected Deployment |
| `memory_limit_oom` | CORRECT / `Pod/payment-service-85b5dff97c-kw6j4` / AMBIGUOUS | CORRECT / `Pod/payment-service-59f849cfbc-rvcr4` / AMBIGUOUS | HARNESS_VARIANCE — Pod remains owned by expected Deployment |
| `payment_pod_crash` | CORRECT / `Pod/payment-service-675bdc99d9-c5cqm` / INSUFFICIENT_EVIDENCE | CORRECT / `Pod/payment-service-789b4c5599-tqk67` / INSUFFICIENT_EVIDENCE | HARNESS_VARIANCE — new Pod identity; resolution unchanged |
| `payment_config_cross_service_impact` | CORRECT / `Deployment/payment-service` / AMBIGUOUS; `SPEC_CHANGE` | CORRECT / `Deployment/payment-service` / AMBIGUOUS; `SPEC_CHANGE`, two `FAILURE_EVENT` findings | LIVE_VARIANCE — extra observed failure findings did not change outcome or resolution |

All other scenario outcome, actor, resolution, confidence, Finding-kind, and
support-status fields match the historical result. Incident IDs and alert
latencies are run-specific.

## Authority and evidence limitations

Benchmark setup/teardown intentionally changed test workloads and staged
faults; these harness writes are part of the suite. The RCA/control-plane path
remained read-only: `make rbac-check` passed, and its service account cannot
GET Secrets, PATCH Deployments, or DELETE Pods. No investigation-originated
workload/application mutation or autonomous remediation was observed. Secret
access was 0. No out-of-policy action executed. The API server did deny optional
Chaos Mesh custom-resource LIST probes with 403; these were rejected reads,
not successful access or mutation.

The control-plane also logged that bounded Loki reads were skipped because the
two-hour incident lookback exceeded the Loki reader's one-hour query bound
(`ValueError: Loki query requires an ordered timezone-aware window <= 3600
seconds`). Diagnoses completed without those log observations. This run does
not validate Loki acquisition for the full incident lookback, and no production
code was changed in this re-anchor.

## Validation and safety

`make live-scenarios` confirmed the same 25 scenario IDs as the historical
suite: 16 root-cause expectations and 9 abstention expectations. `make
rbac-check` passed both before and after the run. The RCA service account can
read the required resources but cannot GET Secrets, PATCH Deployments, or
DELETE Pods. The optional Chaos Mesh LIST requests mentioned above were denied
by the API server (403); no out-of-policy action executed successfully.

The benchmark harness intentionally stages and tears down faults, restarts or
scales workloads, and generates workload traffic. Those are test-harness
operations, not RCA/control-plane remediation. Diagnosis requests themselves
were reads; investigation-originated workload/application writes, Secret
access, and autonomous remediation were 0.

Repository validation after the result and README edits:

- `make check`: PASS — Ruff, format check, mypy (207 source files), and pytest
  (873 passed; one third-party deprecation warning).
- `make precommit`: PASS.
- `make rbac-check`: PASS.

No production RCA code or historical result artifact changed during this
re-anchor. The `m16.v1` contract was not changed.

```text
Code: da6f0e6
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

Median alert time: 25.1s
p95 alert time: 60.3s
```

## Gate

| Gate | Pass | Value |
|---|:---:|---|
| Harness: 0 NO_INCIDENT / NO_DIAGNOSIS / ERROR | ✅ | 0 failures |
| Safety: 0 confident fabrication | ✅ | 0/9 |
| Root actor >= 15/16 | ✅ | 16/16 |
| Overall >= 24/25 | ✅ | 25/25 |
| Model calls: 0 | ✅ | 0 |

## Per-scenario

| Scenario | Expected | Outcome | Resolution | Actual | Alert s |
|---|---|---|---|---|---:|
| `payment_error_spike` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/payment-service-659c86bc64-jt2km` | 25.1 |
| `order_error_spike` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-568775468b-6mswv` | 25.1 |
| `payment_dependency_latency` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-568775468b-6mswv` | 30.2 |
| `order_latency_spike` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-568775468b-6mswv` | 20.2 |
| `payment_db_pool_pressure` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/payment-service-659c86bc64-jt2km` | 40.2 |
| `order_db_query_latency` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-568775468b-6mswv` | 20.1 |
| `cross_service_payment_failure` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-568775468b-6mswv` | 25.1 |
| `cross_service_db_pressure` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/order-service-568775468b-6mswv` | 25.2 |
| `payment_db_query_latency` | ABSTAIN | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/payment-service-659c86bc64-jt2km` | 25.1 |
| `order_worker_lag` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/order-worker` | 40.2 |
| `order_worker_failure` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/order-worker` | 85.4 |
| `payment_config_change` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/payment-service` | 15.1 |
| `order_config_latency` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/order-service` | 20.1 |
| `payment_config_cross_service_impact` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/payment-service` | 25.1 |
| `dependency_endpoint_misconfig` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/order-service` | 30.2 |
| `configmap_dependency_change` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/ConfigMap/workload-config` | 40.2 |
| `image_regression` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Pod/payment-service-565dd98c-rhmtg` | 30.7 |
| `dependency_scaled_to_zero` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/payment-service` | 25.1 |
| `rollout_restart_disruption` | root-cause | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Deployment/payment-service` | 25.1 |
| `network_policy_isolation` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/NetworkPolicy/deny-order-egress` | 50.3 |
| `memory_limit_oom` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Pod/payment-service-59f849cfbc-rvcr4` | 30.2 |
| `cpu_limit_throttle` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/payment-service` | 25.6 |
| `service_selector_drift` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Service/payment-service` | 20.2 |
| `worker_deployment_deleted` | root-cause | CORRECT | AMBIGUOUS | `sre-demo/Deployment/order-worker` | 60.3 |
| `payment_pod_crash` | root-cause | CORRECT | INSUFFICIENT_EVIDENCE | `sre-demo/Pod/payment-service-789b4c5599-tqk67` | 0.0 |
