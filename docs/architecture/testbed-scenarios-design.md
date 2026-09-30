# Testbed scenarios and runs (roadmap B4)

Status: **APPROVED IN SLICES** (2026-09-30). The owner chose sliced delivery (§10); the decisions of §9 apply to the
slice that is being built, and the decoy amendment waits until the negative control joins. Builds on `testbed-ground-truth-contract.md`
(what a run records and how it is scored), `testbed-lab-design.md` (the lab) and
`testbed-control-plane-design.md` (the control plane outside it).

## 1. Purpose

Turn the lab into a measurement: a fixed set of controlled incidents whose truth is known and recorded,
each repeated with seeded variation, split into development and held-out runs before the first run, and
scored on the metrics of the ground-truth contract §7. The result is the **frozen baseline** of the current
engine (roadmap B5); it is not a tuning set for the held-out part.

## 2. What the lab offers (measured or read from the repository)

- **Call graph.** `order-service` calls `payment-service` (HTTP `POST /payments`), writes to Postgres and
  produces to Kafka `orders.created`; `payment-service` writes to Postgres; `order-worker` consumes the
  topic. Client traffic enters at `POST /orders` (the load generator).
- **Alerts** that fire on latency and errors (`PaymentRequestLatencyHigh`, `OrderDependencyLatencyHigh`,
  `OrderRequestLatencyHigh`, `PaymentDbQueryLatencyHigh`, `HighRequestLatency`, `KafkaConsumerLag`,
  `PodRestartSpike`, the error-rate alerts). A `NetworkChaos` delay on `payment-service` under load fired a
  first alert after 30 s and the chain after 45 s.
- **Faults.** Chaos Mesh (`NetworkChaos`, `PodChaos`, `StressChaos`, `Schedule`), configuration and rollout
  changes through the existing live actions, and in-process faults through `POST /__faults` (no cluster trace).
- **Isolation object.** `lab-control/isolated-echo`, with a default-deny NetworkPolicy that `make lab-check`
  verifies.

## 3. Run isolation (replaces the old truncation)

The existing live runner empties the control plane's journal tables before every scenario through the
in-cluster Postgres. That path no longer applies (the control plane is outside the lab) and it would destroy
the history we chose to keep. Each run therefore gets, instead:

1. a **fresh control-plane database** for the run (created in the control plane's Postgres, migrated, and
   the control plane restarted on it); earlier runs' databases stay;
2. a **restart of the connector** (new epoch, empty alert buffer), so alerts of an earlier run are never
   replayed into the new database as incidents;
3. **quiescence** before injecting: no firing alert, no chaos object, workloads at their baseline.

Nothing is truncated. The change journal of the run's database starts empty, so the only changes the engine
can find are the run's own. Estimated cost: about a minute per run.

**Observed on 2026-09-30 while verifying the UI notifications:** after the lab was recreated, the control
plane's journal (kept across the recreation on purpose) recorded the new cluster's `kube-root-ca.crt`
ConfigMaps as **updates** (same key, new content) about 30 minutes before the next incident, and the engine
then named `ConfigMap/kube-root-ca.crt` as the leading root actor of all six new incidents ("Ambiguous",
"Likely") although the chaos experiment's own events (29 of them, including `Applied` and `Recovered`) had
reached the journal. Before the recreation the same fault was attributed to the experiment. This is exactly
the confound that a fresh database per run removes, and it is also a finding about the engine itself: a stale
unrelated change outranked a fault applied inside the incident's window. It is not fixed by name (the
causal semantics contract forbids keying on these objects); phase 0 must include this case.

## 4. Scenarios

Two variants per family (a `scenario_id` each); the split of §6 assigns each variant to `DEV` or `HOLDOUT`.
"Expected" states a hypothesis to be **checked by the run**, never a target to tune toward; B5 freezes the
engine before any of it is measured.

| Family | Injection (variant A / B) | True chain | Symptom the oracle watches | Expected of the current engine |
|---|---|---|---|---|
| `direct-pod-fault` | `StressChaos` (CPU) on `payment-service` / on `order-service` | cause = the experiment; execution = its target pod; effect = same workload | latency of the faulted service itself | names the experiment; effect link only if the pod is a declared symptom |
| `dependency-fault` | `NetworkChaos` delay on `payment-service` (300 to 600 ms) / packet loss | cause = the experiment; effect at `payment-service`; propagation to `order-service` | `order-service` request latency and errors | names the experiment; **propagation recall 0** (the service-level relation is not implemented, roadmap C1) |
| `scheduled-recurring` | a chaos `Schedule` spawning a 20 s `NetworkChaos` delay every minute on `payment-service` / a 20 s `StressChaos` every minute on `order-service` | cause = the Schedule instance; executions = its experiments (each with its own UID) | as the matching single fault | one supported family through the spawn explanation; Schedule instance named |
| `config-or-rollout` | `FAULT_PAYMENT_DELAY_MS` set on `payment-service` through an env change (600 to 1500 ms) / an image change to a tag that does not exist plus a pod delete, so payments fail (both are existing live actions) | cause = the Deployment change; execution = the new ReplicaSet and pods | `order-service` latency | D1 support only (no strong rule for rollouts yet, roadmap C5); variant B raises error alerts rather than latency |
| `negative-control` | the **real** config change above **plus** a decoy: a chaos fault in `lab-control` started at about the same time | cause = the config change; the decoy is listed as a decoy; construction = "no call edge from `lab-control`, NetworkPolicy default-deny, verified by `lab-check`" | as `config-or-rollout` | must **not** name the decoy, and holds no strong claim on it |
| `competing-causes` | two independent causes with separable symptoms: a delay on `payment-service` **and** a `pod-kill` of `order-worker` | two cause links with two chains (latency alerts; `KafkaConsumerLag`) | both alert groups | scored per cause; neither may absorb the other |

## 5. Parameters, timing, repeats

- **Protocol per run:** 60 s quiet baseline with the oracle sampling; injection after a random offset of 0
  to 30 s; the fault lasts 100 to 140 s; then the cause is removed, and the run waits for recovery and for the
  stored diagnosis (up to 300 s after the first alert), plus a 60 s cool-down. About eight minutes.
- **Seeded randomization** (one seed per repeat, in the manifest): the injection offset, the duration, the
  magnitude (latency or loss percentage, CPU load), which variant target applies, and the load rate (8 to 14
  requests per second).
- **Repeats:** 5 per scenario. Twelve scenarios give 60 runs, about eight hours.
- **Phase 0 (validation), before the manifest is frozen:** one run per family, about fifty minutes. Its only
  question is whether the oracle can derive every required timeline field for that family in this lab; the
  network probe problem below is the main risk. Nothing from phase 0 enters the scored runs.

**Probe validity (the main known risk).** `kubectl port-forward` reaches a pod over its loopback interface, so a
`NetworkChaos` delay applied to the pod's network device may not affect a probe that goes through a
port-forward. The end-to-end probe (`POST /orders` through `order-service`, which calls `payment-service` over
the pod network) does see it. The effect **at the target** therefore has to be measured on a path that really
crosses the faulted interface; phase 0 must show that each family's target probe degrades under its own fault,
or the scenario is redefined or dropped before freezing.

## 6. Split, manifest, acceptance

- **Tier** per scenario from `assign_tier(salt, scenario_id)` with a salt chosen and recorded in the manifest
  before any run; the manifest is frozen with its digest and the engine version (`testbed.suite.v1`).
- **Acceptance thresholds**, fixed in the manifest and reported pass or fail, not tuned: `false_resolved` = 0,
  `false_strong_authority` = 0, and at least 90% of runs valid. Every other metric of §7 is reported
  without a threshold (this is the baseline).
- **Amendment to the ground-truth contract** (needed for the negative control): the chain gains an optional
  list of **decoys** (actors injected but unrelated); the scorer adds `decoy_named` (a supported or strong claim
  on a decoy); `abstained` applies only to a run whose chain has no cause.

## 7. The runner

A new `testbed_runner` in `packages/evals/live` (the existing suite stays as it is) that, for each manifest
scenario and repeat, performs the isolation of §3, executes the protocol of §5 while writing the injector
journal and the oracle series, reads the run's own database for the stored diagnosis, assembles and stores the
`RunRecord`, and scores it. It reuses the existing actions, the workload driver and the port-forward
supervisor.

## 8. Not in scope

Choosing acceptance thresholds for the ungated metrics; any engine change (C-items); the multi-cutoff
recordings of roadmap B6 (they attach to these runs later); running the suite (roadmap B5 freezes first).

## 9. Decisions requested

1. The isolation of §3: a fresh control-plane database and a connector restart per run, nothing truncated.
2. The six families and twelve scenarios of §4, with the negative control defined as a real change plus a
   decoy, and the contract amendment of §6 (decoys, `decoy_named`, `abstained` only without a cause).
3. The protocol timing, parameter ranges and five repeats of §5.
4. Phase 0 as a gate: a scenario whose target probe does not degrade under its own fault is redefined or
   dropped before the manifest is frozen.
5. Tiers by salted hash and the acceptance thresholds of §6.
6. The runner of §7 as a new module next to the existing live suite.

## 10. Delivery in slices (owner decision, 2026-09-30)

The full list of §4 and §5 stays the target. Delivery is staged so that the riskiest part, the runner and the
validity of the probes, is tested first and the suite grows only on evidence.

1. **Slice 1: `dependency-fault`, `payment-service` delay.** One scenario, three repeats, `DEV`. The runner is
   built for it; a **phase 0** run (not scored, stored apart from the suite) shows whether the oracle derives
   every timeline field; only then is the manifest frozen and the three repeats run. Per-run isolation of §3 is
   part of the slice.
2. **Then, one family at a time,** each with its own phase 0: `direct-pod-fault`, `config-or-rollout`,
   `scheduled-recurring`, `competing-causes`, and last `negative-control` (which needs the decoy amendment of
   §6, asked for then).
3. **The tier split** applies once the suite has more than one scenario; slice 1 is declared `DEV` in its
   manifest.

Rules made concrete for the slice (open to amendment here):

- **Primary incident.** One fault raises several incidents (four to six alerts of one chain). A run is scored on
  the **earliest incident created after the injection whose alert belongs to the scenario's expected alerts**,
  using its latest stored diagnosis; every stored diagnosis of the run is kept as an artifact for later analysis.
- **Probes.** The *target* probe is an HTTP request to `payment-service` made **from inside an `order-service`
  pod** (it crosses the faulted network device; a host `port-forward` does not). The *propagation* and
  *symptom* probes are one client-facing `POST /orders` through a port-forward, judged against two thresholds:
  a threshold calibrated on the quiet baseline of that run (propagation), and the 0.5 s of the alert rules
  (symptom). Calibration values are written to the injector journal.
- **Chain of the slice:** cause and execution = the experiment (its UID), target effect = the `payment-service`
  pod (its UID), propagation = the `order-service` Deployment, symptom = the `order-service` service.

### Phase 0, first run (2026-09-30)

All seven timeline fields were derived and the target probe (a request to `payment-service` from an
`order-service` pod) degraded within a sample of the injection, so the main risk of §5 did not materialize for
this family. The run was `INVALID` for a measurement reason (an event timestamp one-second resolution, see the
ground-truth contract §12) and exposed three further things, all corrected in the harness: sequential probing,
stamping at sample start, and lingering events from earlier experiments reaching the run.

**An engine finding, not fixed here.** The stored diagnoses named `NetworkChaos/cp-gate-delay`, an experiment from
an earlier manual run that had ended about 40 minutes before the incident, as the root cause, and gave it
**strong** authority (`m21.support.observed-fault-execution`) through an execution interval of 11:36:03 to 11:37:22
and a pod-failure observation inside it, while the real experiment of the run was only plausible. The rule does
not require the execution interval to be connected to the incident's onset. That is a false strong authority of
exactly the kind the acceptance criterion `false_strong_authority = 0` exists to catch, and it is a defect of the
rule of `m21-causal-semantics-contract.md` (the fault-execution paragraph), not a limitation of the data.

### Slice 1 result (2026-09-30)

Suite `slice1` (manifest `064176e7…`, engine 2.1.0, seeds 11, 12, 13, `DEV`): three valid runs, `false_strong_authority`
0, `false_resolved` 0. Rescored under the amendment of the ground-truth contract §13 (the first scores counted
structural `change-onset-path` witnesses as execution and effect, and read the links from one incident whose choice
depended on which alert arrived first):

| Metric | First score | Rescored |
|---|---|---|
| cause / instance recall | 3/3, 3/3 | 3/3, 3/3 |
| execution witness recall | 3/3 | **0/3** |
| effect link recall | 2/3 | **0/3** |
| propagation link recall | 1/3 | not measured |
| median time to diagnosis | 96.5 s | 96.5 s |

The engine names the injected experiment (and its exact instance) as a possible initiating cause in every run and
never gives it strong authority: `m21.support.observed-fault-execution` is `NOT_FIRED`
(`NO_EXECUTION_WITH_INCIDENT_EFFECT_WITNESS`) in all 10 stored diagnoses. Why it does not fire on a real run is the next
question (engine investigation, not a scorer matter). Run 2's different first incident (`OrderDependencyLatencyHigh`
before the payment alerts, two incidents instead of four) is alert ordering in the lab, now neutral to the links.

### Why the fault-execution rule did not fire in slice 1 (2026-09-30, read-only investigation)

From the stored diagnoses and each run's own database (`testbed_slice1_dependency_delay_payment_0..2`):

1. **The delay leaves no pod-level failure observation (runs 0 and 1).** The rule needs a failure observation of the exact
   target pod (`CONTAINER_FAILURE`, `RESOURCE_PRESSURE`, `DEPENDENCY_ERRORS`, `FAILURE_EVENT`) inside
   `[Applied, Recovered]`. A 300 to 600 ms network delay produced none on `payment-service` (no `Unhealthy`, no restart);
   its effect is latency, which is not an effect kind of the claims. Run 0 and run 1 held no payment-pod claim with a
   failure finding. This is the known limit of roadmap C2 (first target-local effect), not a defect of the rule.
2. **A repeated event carries its earlier history (run 2).** The payment pod did fail its liveness probe under the
   delay (`Unhealthy` x28), so a pod claim with an effect existed. But the Kubernetes events are recurring
   aggregates whose `firstTimestamp` (11:37:18 and 12:16:51) comes from earlier experiments on the same, long-lived pod
   (the kubelet re-creates a deleted event with its cached first timestamp and count). The rule treats every instant of
   an observation as covered and refuses an effect that may precede `Applied`, so it stayed `NOT_FIRED`. It is doing
   what it was written to do; the harness gives the pod a history.
3. **Run 0's stored diagnosis predates the closing event (unverified cause).** Its `Recovered` event exists in the run's
   database (12:40:29) but the latest stored diagnosis (12:40:45) still had `recovered_at: null`; an interval whose end
   is unobserved proves nothing. Not investigated further (ingestion lag or revision timing).

Consequences, none acted on: the rule's non-firing here is explained by observability (1) and by the harness (2, 3),
so no rule change is indicated, and the engine must not be adjusted to make slice 1 fire. Options for the owner:
a fresh `payment-service` pod per run in the isolation step (removes 2), and re-reading the incident's diagnosis after
recovery before storing it (addresses 3). Both are harness changes and would need slice 1 to be re-run as a new suite.

### Baseline health (owner-approved, 2026-09-30)

**Diagnosis.** After seven phase-0 runs of `direct-pod-fault`, an offline audit of every recorded run database
showed the problem was the world, not the rule or the fault intensity: the target services failed their probes
under the **ordinary test load**, before any fault. `m21.support.observed-fault-execution` refuses an effect
that may precede `Applied`, and it refused exactly where the target pod held a warning older than the
injection:

| Run | Valid | Target-pod warnings before `Applied` |
|---|---|---|
| slice1 #0, #1 | yes | 0 (the delay left no pod failure at all) |
| slice1 #2 | yes | 15 |
| phase0 direct (order-service), two runs | yes | 5 and 9 |

For example, in the last one the fresh `order-service` pod failed its liveness probe at 14:33:40 and its
readiness probe at 14:34:10, and the experiment applied at 14:34:48. The audit also found two isolation leaks:
events re-created by the kubelet with their cached first timestamp (9 of 13 databases), and the previous
connector's buffer delivered to a new run's database (2 databases; fixed by restarting the connector before the
control plane).

**Changes.**

1. **Headroom, lab only.** `make lab-tune` (part of `lab-up`) raises the probe timeout of `order-service` and
   `payment-service` from 1 s to 3 s through `infra/kubernetes/lab-workload-patch.yaml`; `workload.yaml` stays as
   the demo defines it. Measured under the test load (about 12 requests per second, three minutes, no fault):
   no warning on either service. Under a 24-worker CPU stress on `order-service`: request median 0.9 s and
   both probes failing, no restart. Raising the CPU limit to 1000m was tried and rejected: the stress then
   crossed neither the alert nor a probe.
2. **A gate before the injection.** If the target pod holds any warning event after the baseline, the run is
   refused before anything is injected (`BaselineNotQuiet`), nothing is recorded as a run, the work directory is
   kept aside, and a suite stops rather than retrying until the world happens to be quiet. Applied
   retrospectively, the gate refuses exactly the runs the rule refused for this reason.

**Method.** Debugging by one eight-minute live run per hypothesis was slow and read each run's symptom
instead of its cause; the audit above took minutes over data already recorded. Such audits come first from now
on, and a live run confirms a fix.

### Confirmation run after the baseline fix (2026-09-30)

Valid, the gate passed (no warning on the target before the injection), cause and exact instance named, no false
strong authority; the fault-execution rule still did not fire. Read from the run's own database, two blockers
remain, and neither is a harness defect:

1. **The closing event lost a race with the incident's resolution by 0.17 s.** `Recovered` happened at 15:02:59
   and reached the control plane at 15:03:14.681 (the connector's event path lags about 15 s); the incident
   resolved at 15:03:14.508 and its window froze there, so every later diagnosis, including the one asked for
   after recovery, correctly treats the interval as unclosed. The alert's resolve delay and the event lag are of
   the same size, so this is a coin toss per run.
2. **A CPU stress leaves its effect as a metric, not as an event of the pod instance.** With the probe headroom
   the stressed pod failed no probe in this run; its only effect finding was `RESOURCE_PRESSURE` (throttling 22% to
   100%), which the engine does not bind to a pod instance, so no exact Pod claim carried an effect.

Both are properties of the engine and the product path (roadmap C2 and the evidence-arrival timing of a resolved
incident), not of the lab, and are left for decisions rather than tuned around in the harness.
