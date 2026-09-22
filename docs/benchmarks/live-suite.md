# The internal live scenario suite

25 scenarios that stage a real fault in a running cluster, let the real
alerting path open an incident, and grade the diagnosis the product actually
stored. Nothing here replays a snapshot.

This is the counterpart to ITBench-Lite, not a replacement. ITBench-Lite
measures root-cause accuracy against an external label on frozen data. This
suite measures whether the whole pipeline — fault, metric, alert, incident,
change journal, diagnosis — holds together on live infrastructure, against
labels we set ourselves. Its labels have no external validity, so the two
numbers are never added together.

## Running it

Needs a deployed cluster (`make deploy`). Port-forwards are opened and closed
by the runner; an already-open port is reused, so this composes with the
`make ui` forward.

```bash
make live-scenarios
```

```bash
make live-bench
```

```bash
make live-bench-dev
```

Results land in `.local/live-bench/`. The runner restores the namespace after
each scenario, and `make live-restore` undoes anything a cancelled run left
behind.

## What is graded

Every scenario carries one of two expectations, and both count equally.

**`RootCause`** — the fault leaves a durable Kubernetes trace (a spec edit, a
ConfigMap change, a new NetworkPolicy, a scale, a deletion). The engine must
name the actor that changed. A Pod or ReplicaSet owned by the expected
Deployment counts as the same answer, because to an operator it is.

**`Abstain`** — the fault lives only in a service process's memory, injected
through `POST /__faults`. No change exists anywhere in the cluster. The engine
must name nothing.

Grading abstention as a success is the deliberate part. A change-first engine
that produces a confident root cause when no change occurred has not made a
near miss; it has fabricated an answer, and an operator who trusts it will
roll back something innocent. The suite reports fabrications as their own
count for exactly this reason.

Outcomes: `CORRECT`, `WRONG_ACTOR`, `FABRICATED`, `MISSED`, `NO_INCIDENT`,
`NO_DIAGNOSIS`, `ERROR`. The last three are harness failures and are excluded
from the denominator — a scenario whose alert never fired measures the cluster,
not the engine.

A correct actor backed by none of the expected finding kinds still passes, but
is flagged in the result (`supported_by_expected_kind`). The right answer for
the wrong reason is worth knowing about.

## Composition

`make live-scenarios` prints the current composition; this table is a snapshot
of it.

| | Root cause expected | Abstention expected | Total |
|---|---|---|---|
| DEV | 13 | 6 | 19 |
| HOLDOUT | 3 | 3 | 6 |
| **Total** | **16** | **9** | **25** |

The `demo`-marked scenarios stage a visible change the engine is expected to
find, and are the ones worth showing in the UI.

### Fault mechanisms

| Mechanism | Finding the engine should see |
|---|---|
| `kubectl set env` on a Deployment | `SPEC_CHANGE` |
| ConfigMap patch | `CONFIG_CHANGE` |
| Image tag change | `IMAGE_CHANGE` |
| Scale a dependency to zero | `SCALE_CHANGE` |
| Rollout restart with a forced pod gap | `ROLLOUT_RESTART` |
| NetworkPolicy create | `POLICY_CREATED`, `NETWORK_RESTRICTION` |
| Memory / CPU limit that starves the container | `SPEC_CHANGE` |
| Service selector edit | `SPEC_CHANGE` |
| Deployment delete | `OBJECT_DELETED` |
| Forced process restart | `CONTAINER_FAILURE`, `FAILURE_EVENT` |
| `POST /__faults` | *(nothing — abstention expected)* |

### What the platform cannot stage

The suite runs on a single-node `kind` cluster with no metrics-server and an
I/O-bound workload, and every alert is metric-based. Three fault classes cannot
be exercised reliably here and are deliberately absent rather than shipped
flaky:

- **Resource-contention latency.** A CPU limit does not raise latency on an
  I/O-bound app, and blocked scale-ups add no latency while the existing pod
  serves. The memory- and CPU-limit scenarios therefore starve the container
  outright and are observed through the dependent service's errors, not through
  a latency threshold.
- **Autoscaling failure.** An HPA needs metrics-server to act; without it a
  `maxReplicas` cap is inert and raises no incident.
- **A container that crashes at startup.** It never serves `/metrics`, so a
  metric-based alert cannot see it. Such faults are staged so that a *dependent*
  service, which is up and recording errors, makes them observable.

## Holdout discipline

The 6 `HOLDOUT` scenarios exist to keep the DEV set from being fitted. They
are not used while changing engine behaviour, and a reported number states
which tier produced it. The tiers are disjoint, and both tiers contain
scenarios of both expectation classes — a holdout that only tested one axis
would measure half the thing.

## Provenance

These scenarios descend from the V020 and A1G fixtures deleted in commit
`8057277` (2026-09-17). The fault parameters, alert mappings, and timing
constants are ported from `packages/evals/live_fixtures.py` on the
`archive/experiments-2026-09` branch. The 2101-line runtime around them was
not ported: it depended on modules that no longer exist, and the declarative
table in `packages/evals/live/scenarios.py` replaces it.

The A1G scenarios were remapped onto the generic alert rules already deployed
in `infra/kubernetes/observability.yaml`, so the five `A1G*`-prefixed alert
rules from the archive are not needed. A unit test asserts that every
scenario's alert actually exists in the deployed rules, because a scenario
whose alert was never provisioned would silently report `NO_INCIDENT` forever.

## Using a scenario in the UI demo

```bash
make live-demo SCENARIO=payment_config_change
```

This stages the fault and skips teardown, so the incident and its diagnosis
stay on screen at `make ui`. The suite's `demo` scenarios are the ones where
the engine is expected to produce an answer; the abstention scenarios show the
opposite behaviour and are better framed as such than shown cold.

```bash
make live-restore
```
