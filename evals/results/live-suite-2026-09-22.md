# Live scenario suite — first full run

**Date:** 2026-09-22
**Code:** `637e20c` (scenario definitions and runner)
**Cluster:** single-node `kind` (`agentic-sre`), no metrics-server, deterministic
demo workload; every alert metric-based.
**Command:** `make live-bench`
**Per-scenario data:** [`live-suite-2026-09-22.json`](live-suite-2026-09-22.json)

## Headline

**24 / 25 correct (96.0%), 0 fabrications, 0 ungraded.**

Every scenario staged a real fault, the real alerting path opened an incident,
and the control plane's stored diagnosis was graded against a label set before
the run. No scenario failed to produce an incident, and no abstention scenario
was fabricated against.

| | Scenarios | Correct |
|---|---|---|
| DEV | 19 | 18 |
| HOLDOUT | 6 | 6 |
| **Total** | **25** | **24** |

By expectation class:

| | Scenarios | Correct | Notes |
|---|---|---|---|
| Root cause expected | 16 | 15 | 15 named the right actor as the leading hypothesis (`AMBIGUOUS`); 1 wrong actor |
| Abstention expected | 9 | 9 | none reached a confident (`RESOLVED`) verdict |

## What "correct" means here

- An **abstention** scenario stages a fault that leaves no cluster change
  (`POST /__faults`). It is correct when the engine does **not** reach a
  `RESOLVED` verdict. All 9 returned `INSUFFICIENT_EVIDENCE`; several surfaced a
  failing pod or a recently-rolled ReplicaSet as a weak candidate, which is
  honest localization off a real signal, not a fabricated change-based cause.
- A **root cause** scenario stages a durable change. It is correct when the
  engine names that actor (a Pod or ReplicaSet owned by the expected Deployment
  counts). 15 of 16 did, all at `AMBIGUOUS` — the right actor led, but the
  resolver did not promote it to a single confident answer.

No scenario reached `RESOLVED`. That is the resolver's current behaviour on live
data, not a suite artifact: it surfaces a correct leading hypothesis but holds
short of a confident verdict. The suite measures whether the leading actor is
right; the separate resolver work is what would move `AMBIGUOUS` to `RESOLVED`.

## The one miss

`rollout_restart_disruption` — expected `Deployment/payment-service`, named
`ReplicaSet/order-service-…` at `INSUFFICIENT_EVIDENCE`.

This is the subtlest scenario: a payment rollout restart with a forced pod gap,
observed through the order service's errors. The engine did not fabricate — it
returned `INSUFFICIENT_EVIDENCE` and offered a weak candidate. The candidate it
offered is the order-service ReplicaSet that the harness's own baseline reset
had just rolled, which sits in the window as a competing structural change. The
same scenario graded correct in an earlier isolated run, so the attribution is
non-deterministic between the payment restart and the baseline-induced
order-service ReplicaSet. It is the weakest scenario in the suite and the honest
edge of what a rollout-restart signal can be attributed to.

## Timing

Median time from staging to incident was ~30s. The slowest were
`order_worker_failure` (90s, Kafka consumer-error accrual) and
`worker_deployment_deleted` (65s, lag build-up); `payment_pod_crash` was
effectively immediate (the restart fires the instability alert directly).

## Reproducing

```bash
make deploy
make live-bench
```

The runner opens and closes its own port-forwards, resets the change journal per
scenario, and restores the namespace after each. `make live-bench-dev` and
`make live-bench-holdout` run one tier at a time.
