# Live scenario suite — full run report

**Date:** 2026-09-22
**Code:** `637e20c` (scenario definitions and runner)
**Cluster:** single-node `kind` (`agentic-sre`), no metrics-server, deterministic
demo workload; every alert metric-based.
**Command:** `make live-bench`
**Model calls:** 0 — the deployed control plane has `SRE_LLM_ENABLED` unset; all
386 stored diagnoses are `mode=deterministic, model_calls=0`.
**Per-scenario data:** [`live-suite-2026-09-22.json`](live-suite-2026-09-22.json)

## Headline

**24 / 25 correct (96.0%), 0 fabrications, 0 ungraded.**

Every scenario staged a real fault, the real alerting path opened an incident,
and the control plane's stored diagnosis was graded against a label set before
the run. No scenario failed to produce an incident, and no abstention scenario
was fabricated against.

| Tier | Scenarios | Correct |
|---|---:|---:|
| DEV | 19 | 18 |
| HOLDOUT | 6 | 6 |
| **Total** | **25** | **24 (96%)** |

By expectation class:

| Class | Scenarios | Correct | Notes |
|---|---:|---:|---|
| Root cause expected | 16 | 15 | 15 named the right actor as the leading hypothesis (`AMBIGUOUS`); 1 wrong actor |
| Abstention expected | 9 | 9 | none reached a confident (`RESOLVED`) verdict |

## How a run works

`make live-bench` runs `scripts/live_benchmark.py`, which opens its own
port-forwards and processes the 25 scenarios in order. For each scenario the
runner:

1. refreshes the workload port-forwards (a prior rollout leaves them stale);
2. restores a clean baseline (`/__faults` cleared, fault env vars unset);
3. waits for the scenario's alert to clear in Prometheus, so the next firing is
   a real not-firing → firing transition Alertmanager will deliver;
4. truncates the change journal (`object_versions`, `event_versions`,
   `change_records`) and takes a fresh baseline snapshot, so the fault it is
   about to stage is the only change inside the engine's two-hour window;
5. stages the fault (the scenario's `setup` actions);
6. drives sustained background traffic until the incident opens;
7. waits for the incident whose title matches the alert and whose `updated_at`
   is at or after the staging moment;
8. reads the stored diagnosis and grades it;
9. tears the fault down and restores the namespace.

The runner never tells the engine what was staged. The real path runs on its
own: Prometheus alert → Alertmanager → control-plane webhook → incident →
auto-diagnosis.

## Ground truth

Unlike ITBench-Lite, whose labels are external (IBM), the label for each live
scenario is set in-house, in one of two classes:

- **`RootCause`** — the fault leaves a durable Kubernetes change, so the correct
  answer is the actor that changed. Because the harness itself makes the change,
  the ground truth is known exactly. A Pod or ReplicaSet owned by the expected
  Deployment counts as the same answer.
- **`Abstain`** — the fault lives only in a process's memory (`POST /__faults`)
  and leaves no cluster change, so the correct behaviour is to reach no
  confident (`RESOLVED`) verdict.

These labels have no external validity, so this measurement is never combined
with the ITBench-Lite number.

## Grading

| Outcome | Meaning |
|---|---|
| `CORRECT` (root cause) | named the expected actor |
| `CORRECT` (abstention) | did not reach `RESOLVED` (surfacing a weak candidate under `INSUFFICIENT_EVIDENCE`/`AMBIGUOUS` is honest localization, not a fabricated change-based cause) |
| `WRONG_ACTOR` | named a root cause, but not the staged one |
| `FABRICATED` | reached `RESOLVED` although nothing changed |
| `MISSED` | a change was staged but nothing was named |
| `NO_INCIDENT` / `NO_DIAGNOSIS` / `ERROR` | harness failure — excluded from the denominator |

---

## Abstention scenarios (9) — all correct

Each stages a runtime-only fault through `POST /__faults`; nothing changes in
the cluster. Ground truth is "do not resolve." All nine returned
`INSUFFICIENT_EVIDENCE` — none fabricated a root cause. The weak candidate each
surfaced (a failing pod, or the order-service ReplicaSet the baseline reset had
just rolled) is offered without confidence, which is the intended behaviour.

| # | Scenario | Tier | Alert | Staged | Engine (weak candidate) | Outcome |
|---|---|---|---|---|---|---|
| 1 | `payment_error_spike` | DEV | PaymentErrorRateHigh | payment returns 500 | Pod/payment-service · INSUFF | ✅ CORRECT |
| 2 | `order_error_spike` | DEV | OrderErrorRateHigh | order returns 500 | ReplicaSet/order-service · INSUFF | ✅ CORRECT |
| 3 | `payment_dependency_latency` | DEV | OrderDependencyLatencyHigh | payment +700 ms | ReplicaSet/order-service · INSUFF | ✅ CORRECT |
| 4 | `order_latency_spike` | DEV | OrderRequestLatencyHigh | order +700 ms | ReplicaSet/order-service · INSUFF | ✅ CORRECT |
| 5 | `payment_db_pool_pressure` | DEV | PaymentDbAcquisitionSlow | payment holds the pool | Pod/payment-service · INSUFF | ✅ CORRECT |
| 6 | `order_db_query_latency` | DEV | OrderDbQueryLatencyHigh | order DB query slow | ReplicaSet/order-service · INSUFF | ✅ CORRECT |
| 7 | `cross_service_payment_failure` | HOLDOUT | OrderErrorRateHigh | payment 500 → order errors | ReplicaSet/order-service · INSUFF | ✅ CORRECT |
| 8 | `cross_service_db_pressure` | HOLDOUT | OrderDependencyLatencyHigh | payment pool pressure → order | ReplicaSet/order-service · INSUFF | ✅ CORRECT |
| 9 | `payment_db_query_latency` | HOLDOUT | PaymentRequestLatencyHigh | payment DB query slow | Pod/payment-service · INSUFF | ✅ CORRECT |

---

## Root-cause scenarios (16) — 15 correct, 1 wrong

Each stages a durable change; ground truth is the actor that changed. 15 named
it as the leading hypothesis (`AMBIGUOUS`); none reached `RESOLVED`, which is the
resolver's current behaviour on live data — it surfaces a correct leading actor
but holds short of a single confident verdict.

### Change on the service that is blamed

| # | Scenario | Tier | Staged | Ground truth | Outcome |
|---|---|---|---|---|---|
| 10 | `order_worker_lag` | DEV | worker env: 2 s delay | Deployment/order-worker | ✅ CORRECT |
| 11 | `order_worker_failure` | DEV | worker env: consumer failure | Deployment/order-worker | ✅ CORRECT |
| 12 | `payment_config_change` | DEV | payment env: 10 s delay | Deployment/payment-service | ✅ CORRECT |
| 13 | `order_config_latency` | DEV | order env: 700 ms | Deployment/order-service | ✅ CORRECT |
| 14 | `dependency_endpoint_misconfig` | DEV | order env: wrong payment URL | Deployment/order-service | ✅ CORRECT |
| 15 | `configmap_dependency_change` | DEV | ConfigMap: wrong URL + restart | ConfigMap/workload-config | ✅ CORRECT |
| 16 | `service_selector_drift` | DEV | payment Service selector broken | Service/payment-service | ✅ CORRECT |
| 17 | `network_policy_isolation` | DEV | NetworkPolicy cuts order egress | NetworkPolicy/deny-order-egress | ✅ CORRECT |
| 18 | `worker_deployment_deleted` | HOLDOUT | order-worker deployment deleted | Deployment/order-worker | ✅ CORRECT |

### Change on a dependency, blamed through the dependent service

Payment is taken down or degraded; the order service records the real errors,
and the engine must trace them back to the payment change.

| # | Scenario | Tier | Staged | Ground truth | Outcome |
|---|---|---|---|---|---|
| 19 | `payment_config_cross_service_impact` | HOLDOUT | payment env: 3 s delay | Deployment/payment-service | ✅ CORRECT |
| 20 | `image_regression` | DEV | payment bad image + pod delete | Deployment/payment-service | ✅ CORRECT |
| 21 | `dependency_scaled_to_zero` | DEV | payment scaled to 0 | Deployment/payment-service | ✅ CORRECT |
| 22 | `memory_limit_oom` | DEV | payment 32Mi → OOM + pod delete | Deployment/payment-service | ✅ CORRECT |
| 23 | `cpu_limit_throttle` | HOLDOUT | payment 5m CPU → starved + pod delete | Deployment/payment-service | ✅ CORRECT |
| 24 | `payment_pod_crash` | DEV | payment container restarted ×3 | Deployment/payment-service (Pod matched) | ✅ CORRECT |

### The one miss

| # | Scenario | Tier | Staged | Ground truth | Engine | Outcome |
|---|---|---|---|---|---|---|
| 25 | `rollout_restart_disruption` | DEV | payment rollout restart + pod delete | Deployment/payment-service | ReplicaSet/order-service · INSUFF | ❌ WRONG_ACTOR |

The subtlest scenario: a payment rollout restart with a forced pod gap, observed
through the order service's errors. The engine did **not** fabricate — it
returned `INSUFFICIENT_EVIDENCE` and offered a weak candidate: the order-service
ReplicaSet the harness's own baseline reset had just rolled, which sits in the
window as a competing structural change. The same scenario graded correct in an
earlier isolated run, so the attribution is non-deterministic between the
payment restart and the baseline-induced order-service ReplicaSet. It is the
weakest scenario in the suite and marks the honest edge of what a
rollout-restart signal can be attributed to.

---

## Resolution distribution

No scenario reached `RESOLVED`. That is the resolver's current behaviour on live
data, not a suite artifact: it surfaces a correct leading hypothesis but holds
short of a confident verdict. The suite measures whether the leading actor is
right; moving `AMBIGUOUS` to `RESOLVED` is the separate resolver work.

| Resolution | Count | Where |
|---|---:|---|
| `AMBIGUOUS` | 15 | the correctly-named root-cause scenarios |
| `INSUFFICIENT_EVIDENCE` | 10 | all 9 abstentions + `payment_pod_crash` + the one miss |
| `RESOLVED` | 0 | — |

## Timing

Median time from staging to incident was ~30 s. Slowest: `order_worker_failure`
(90 s, Kafka consumer-error accrual) and `worker_deployment_deleted` (65 s, lag
build-up). Fastest: `payment_pod_crash` (~0 s — the restart fires the
instability alert directly).

## Reproducing

```bash
make deploy
make live-bench
```

`make live-bench-dev` and `make live-bench-holdout` run one tier at a time;
`make live-scenarios` lists the suite. The runner opens and closes its own
port-forwards, resets the change journal per scenario, and restores the
namespace after each. See [the methodology](../../docs/benchmarks/live-suite.md)
for what the single-node platform can and cannot stage.
