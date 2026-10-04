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
| `scheduled-recurring` | a chaos `Schedule` spawning a 60 s `NetworkChaos` delay every 90 s on `payment-service`, in place for three spawns (amended 2026-10-03; the original 20 s every minute never raised a latency alert, see "Slice 4 result") / a 20 s `StressChaos` every minute on `order-service` | cause = the Schedule instance; executions = its experiments (each with its own UID) | as the matching single fault | one supported family through the spawn explanation; Schedule instance named |
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

### Slice 2 result (2026-09-30)

Suite `slice2` (manifest `0ad15b98…`, engine 2.1.0, `direct-stress-order`, seeds 21, 22, 23, `DEV`): three valid runs,
no run refused by the baseline gate, `false_strong_authority` 0, `false_resolved` 0.

| Metric | Result |
|---|---|
| cause / instance recall | 3/3, 3/3 |
| execution witness recall | 1/3 |
| effect link recall | 1/3 |
| propagation link recall | not measured (none in this family) |
| median time to diagnosis | 29 s |

The first strong witness on a real run (repeat 0) was checked against the recorded truth: the exact experiment
instance, an effect at the exact target pod, all four coverage conditions. In all three runs the `Recovered` event
reached the control plane before the incident resolved (the race of the confirmation run went the other way here).
What decided each run was blocker 2: repeat 0's pod failed a probe inside the interval (an event of the pod
instance); repeats 1 and 2 did not, and their only effect was CPU pressure, which is not bound to a pod instance.

### Alert flap census and a stale-alert leak (2026-09-30, offline)

**Census.** Across the 22 recorded run databases there are 32 cases of one alert fingerprint firing again after
it resolved. Gap from resolution to the next firing: one at 6 s (an `OrderDependencyLatencyHigh` flap during a
fault), two at 19.6 s (`KafkaConsumerLag` / `OrderWorkerLagHigh` at load start), and every other case 90 s or
more, where the two firings had different causes (load start versus the end of a run, or two different runs).
In this lab, short gaps (under about 20 s) were continuations of one episode and gaps of 90 s and more were new
episodes; nothing was seen between. The sample is small and lab-specific (the faults are removed after about
100 s, and the `for:` of the lab's rules shapes it), so it bounds the quiet interval but does not choose it; a
soak run is needed for that.

**Leak found.** Alertmanager resends a group's recently resolved alerts with the group's next notification. A
fresh control-plane database takes them as new occurrences and opens incidents for them, created after the
run's injection although the alerts began minutes before it (slice 3 repeat 1: three incidents from repeat 0's
alerts, one of which the harness took as the primary incident; that slice has no valid run, so no score changed).
The harness now ignores an incident whose alert began before the injection. In the product, with one persistent
database, the occurrence key (fingerprint and start) already makes the resend idempotent; a new or restored
database would still open incidents for alerts that arrive already resolved, which is worth a rule of its own.

### Isolation from faults the run did not create (2026-09-30, owner-approved)

A manual diagnostic experiment (`diag-stress`, run while calibrating the CPU stress) entered the evidence of four
phase-0 databases (20 incidents). Every experiment the harness creates now carries the label
`testbed.agentic-sre.io/run` with its run's id. Before the injection the run is refused if any chaos object in the
watched namespaces lacks that label for this run, or any chaos event names an experiment this run did not create;
after the diagnoses are stored the same check runs again, and a finding makes the run `INVALID`. Manual diagnostic
experiments therefore cannot be scored as part of a run, whether they happen before it or during it.

## Slice 4 result and amendment (2026-10-03, owner-approved)

Slice 4 (`scheduled-recurring`, variant A as first designed: a 20 s delay every minute, 300 to 600 ms) failed the
validity bar: **1 of 3 runs valid** (the bar is 90%). The phase 0 run was read as passing because an alert fired;
that alert was `KafkaConsumerLag` / `OrderWorkerLagHigh`, not a latency alert, and the alert's name was not checked.
In phase 0 and all three repeats no latency alert fired: the latency alerts need the 30 s average above 0.5 s for
15 s, about 45 s of continuous delay, which a 20 s spawn never gives. The lag alerts are a side effect (`order-worker`
calls `payment-service`): repeat 0 got one during the fault (valid: cause and Schedule instance named, no false
strong authority, no false `RESOLVED`), repeat 1 none, repeat 2 one 50 s after the Schedule was removed, after the
injector had stopped watching (the harness watches alerts only while the fault is in place; to be fixed separately).
The runs are kept and reported as they are.

**Amendment:** variant A spawns a 60 s delay every 90 s and stays in place 300 to 360 s (three spawns). The spawn
interval and duration are manifest parameters (`spawn_every_seconds`, `spawn_seconds`); a manifest without them,
like slice 4's, keeps 60 s and 20 s. Phase 0 must show a **latency alert by name** before the manifest is frozen.

**Second phase 0 (2026-10-03).** With the amended spawns every spawn raised the four latency alerts
(`PaymentDbQueryLatencyHigh`, `OrderDependencyLatencyHigh`, `PaymentRequestLatencyHigh`, `HighRequestLatency`), but the
run was invalid: `OrderWorkerLagHigh` / `KafkaConsumerLag` fired 46 s after the Schedule was created and 43 s before its
first experiment was applied, so the alert preceded the symptom. **The lag alerts fire without a fault in this lab**
(produced minus consumed over 2 minutes above 10 messages; a 16-message imbalance was enough in the independent run),
which is also a risk for every family that accepts them. Owner-approved: the `scheduled-recurring` family expects the
latency alerts only (its designed symptom); decided before any scored run of slice 4b.

**Slice 4b result (2026-10-03, engine 2.1.0, `DEV`, manifest `dd0603f8…`, seeds 44 to 46).** Third phase 0 valid with
`PaymentDbQueryLatencyHigh` as the first alert (the lag alerts fired again before the Schedule existed). The three
repeats: **3 of 3 valid**, each with three spawned experiments and its first latency alert 119 to 121 s after the
Schedule was created (30 s after the first spawn); cause (the Schedule) and its instance named 3 of 3; **false strong
authority 0, false `RESOLVED` 0, false elimination 0**; execution witness 0 of 3 and effect link 0 of 3 (a delay leaves
no pod-level failure, as in slice 1); median 8 s to the diagnosis. The acceptance bar holds.

## Slice 5 result (`competing-causes`, 2026-10-03, engine 2.1.0, `DEV`, manifest `19e7e570…`, seeds 51 to 53)

Scenario as in testbed contract §15: a 300 to 600 ms delay on `payment-service` and, 0 to 60 s later, a `PodChaos`
pod-kill of `order-worker`. Phase 0 valid (first alert `PaymentDbQueryLatencyHigh`; four latency and two lag
incidents). The three repeats: **3 of 3 valid**; every incident scored against its own group, **group recall 1.0**
(6 of 6 incidents per run), **cross-attribution 0** (the pod-kill was never named for a latency incident), both causes
named in every run; **false strong authority 0, false `RESOLVED` 0, false elimination 0**. Execution witness and
effect link 0 (as slice 1). Instance recall first read 0.5 because instances were taken from the primary (latency)
incident only, where the pod-kill is not a candidate. With instances scored per group (§15.3, owner-approved) and the
runs rescored (`score.v2.json`, nothing re-run): **instance recall 1.0**, both causes' exact instances named in their
own groups' incidents in all three runs.

## Slice 6 result (`negative-control`, 2026-10-03, engine 2.1.0, `DEV`, manifest `2e293cce…`, seeds 61 to 63)

Scenario as in testbed contract §16: slice 3's real configuration change on `payment-service` and a decoy
`NetworkChaos` delay on `lab-control/isolated-echo`, started −30 to +30 s around it. Phase 0 valid with the decoy
3 s **before** the change; the engine saw it (object versions and `Applied` / `Recovered` events journaled), built two
hypotheses on it per incident and kept them `UNLINKED`, admitted only as context, never supported. The three repeats:
**3 of 3 valid**, construction check clean every time, cause and instance named 3 of 3, **`decoy_named` 0**, false
strong authority 0, false `RESOLVED` 0, false elimination 0. The acceptance bar (with `decoy_named` = 0) holds.
Limitation: the seeds drew positive offsets only (decoy 13, 25 and 16 s after the change), so the decoy-first case was
exercised in phase 0 alone, not in a scored run.

## 12. Engine freeze and the first HOLDOUT (2026-10-03, owner-approved)

Status: **APPROVED** by the owner (2026-10-03) and implemented. Roadmap B5 (frozen engine baseline) and E2 (held-out
set).

### 12.1 Why the split of §6 cannot be applied as written

§6 assigns each scenario to `DEV` or `HOLDOUT` by `assign_tier(salt, scenario_id)`. Every scenario run so far (variant A
of each family, slices 1 to 6) was declared `DEV` and its results were seen; several rules were decided on them (the
ordering amendment §14, the scheduled amendment, the competing groups §15, the decoy §16). A salted hash that put any
of them in `HOLDOUT` would label seen data as unseen. The variants B of §4 have never been run.

### 12.2 Proposal

1. **Split by construction, not by hash:** variant A of every family stays `DEV`; variant B of every family is the
   first `HOLDOUT`. The rule is written into the manifests before any variant B runs; `assign_tier` stays for later
   suites built from scratch.
2. **Variants B** (from §4, made concrete; same families, same harness, new injections):

   | Family | Variant B (`HOLDOUT`) | Expected symptom alerts |
   |---|---|---|
   | `dependency-fault` | packet loss (not delay) on `payment-service` | latency and error alerts of `order-service` |
   | `direct-pod-fault` | `StressChaos` (CPU) on `payment-service` (A was `order-service`) | `payment-service` latency |
   | `scheduled-recurring` | `Schedule` spawning a `StressChaos` on `order-service` (60 s every 90 s) | `order-service` latency |
   | `config-or-rollout` | image change to a tag that does not exist plus a pod delete | payment error alerts |
   | `negative-control` | variant B's image change plus a decoy `StressChaos` on `lab-control/isolated-echo` | as `config-or-rollout` B |
   | `competing-causes` | packet loss on `payment-service` plus a pod-kill of `order-worker` (symptom groups as §15) | latency and errors / lag |

   The alert set of each variant is fixed in code and the manifest before its phase 0, not from what phase 0 shows.
3. **Engine frozen by commit:** `RCA_ENGINE_VERSION` stays 2.1.0 (owner decision F1), but that string has not
   identified the code (the §11 presentation change landed after slices 1 to 3). The manifest gains `engine_commit`
   (the git commit of the engine at freeze), and the runner refuses a run whose working tree differs from it in
   `packages/rca` or `apps/control_plane`. No engine change lands until the `HOLDOUT` suite is complete.
4. **Blind phase 0:** each variant B gets a phase 0 that checks only the run's validity and the alert names; the
   engine's diagnosis and score are not printed or read. A variant whose phase 0 fails is redefined, still blind,
   before the manifest is frozen.
5. **Repeats:** 3 per variant (as `DEV`), 18 runs, about four hours. The acceptance bar is the frozen one (no false
   strong authority, no false `RESOLVED`, at least 90% valid; `decoy_named` = 0 for the control).
6. **Use of the result:** reported per family beside `DEV`, never merged. A `HOLDOUT` result never selects a rule or a
   parameter; a failure is recorded as it is, and any fix is measured on a new `HOLDOUT` (new variants or seeds) after
   it.

### 12.3 Decisions requested

- the split by construction (12.2.1) instead of the salted hash for this suite;
- the variants B of the table;
- the engine freeze by commit with the runner's check (a `testbed.suite.v1` field addition);
- 3 repeats (instead of the 5 of §5) for this first `HOLDOUT`.

### 12.4 Blind phase 0, first round (2026-10-03)

Only validity, the timeline, alert names and harness-side series (oracle, Prometheus, journal) were read; no
diagnosis or score of a variant B.

- `dependency-loss-payment`: invalid, propagation before the target effect. The target probe (`/health`) failed only
  now and then (about 1 s TCP retransmissions on a lost packet) and met the three-failures rule at 78.8 s, while the
  client failed almost every sample from 0.3 s. Redefined: the loss variants probe the target with `POST /payments`,
  as the configuration family does.
- `direct-stress-payment`: invalid, no alert fired. Under 20 to 28 workers `payment-service` averaged 0.24 to 0.31 s
  (heavy CPU throttling) and the caller's dependency latency 0.35 to 0.40 s, under every 0.5 s alert threshold.
  Redefined: 56 to 72 workers.
- `scheduled-stress-order`: the run stopped at calibration: every baseline sample failed in under a millisecond
  (`URLError`), so the local port-forward to `order-service` was not serving after the pod was replaced during
  isolation. Rerun unchanged first.

### 12.5 Blind phase 0, second round (2026-10-03)

Valid with their alert names: `scheduled-stress-order` (`OrderRequestLatencyHigh`; the port-forward problem did not
recur), `dependency-loss-payment`, `direct-stress-payment` (payment latency alerts fire at 56 to 72 workers),
`config-image-payment` (`OrderErrorRateHigh`). Invalid:

- `competing-loss-podkill`: the target still met the three-failures rule late (11.9 s against 0.7 s for the client);
  `POST /payments` sees 20 to 40% loss only intermittently as well. Redefined for both loss variants: 50 to 70% loss.
- `negative-image-decoy`: the alert reached Alertmanager (18 s after the change) but no alert or incident reached the
  control plane, although 63 events did; the connector's log of that run was lost to the next run's restart. The
  same change without the decoy (`config-image-payment`) delivered its alert. The CPU-stress decoy is capped at 100m
  by the isolated workload's limit, so a starved node is not the explanation. Rerun unchanged.

### 12.6 Blind phase 0, third round (2026-10-03)

Valid: both loss variants at 50 to 70% (`dependency-loss-payment`, `competing-loss-podkill`). `negative-image-decoy`
was invalid again and was rerun with the control plane's log on: the alert stream was attached, and the only alert
that fired (`OrderErrorRateHigh`) was active for about 20 s. **The lab connector polls Alertmanager every 30 s**
(`SRE_ALERT_COVERAGE_POLL_SECONDS`), so an alert shorter than that can fall between two polls and never reach the
control plane; the injector, polling every 5 s, saw it. The decoy was not the cause (the earlier hypothesis is
withdrawn). The outage was short because, after the image change and the pod delete, the old ReplicaSet brought its
pod straight back; `config-image-payment` passed only because its alert happened to span a poll.

Redefined for both image variants: the broken image goes in one patch with a rollout strategy of `maxSurge: 0`,
`maxUnavailable: 1`, so the old pod goes first and the outage lasts until the change is undone; undoing restores the
image and the original strategy. Both are rerun blind. The short-alert loss is a product finding of its own (alert
coverage; roadmap).

### 12.7 Blind phase 0 complete; freeze

With the lasting outage both image variants are valid (`config-image-payment`: `OrderDependencyLatencyHigh`,
`OrderErrorRateHigh`; `negative-image-decoy` likewise plus the lag alerts) and the deployment's image and strategy are
restored after each. All six variants B passed a blind phase 0; no diagnosis or score of any of them was read. The
engine is frozen at the commit recorded in the six `HOLDOUT` manifests.

### 12.8 First HOLDOUT result (2026-10-03, engine frozen at `96a10c3c`, 18 runs)

Measured once, reported as it is; nothing below selects a rule or a parameter (§12.2.6).

| Family (variant B) | Valid | Cause / instance | Execution witness / effect link | False strong / false `RESOLVED` / false elimination | Other |
|---|---|---|---|---|---|
| `dependency-fault` (packet loss) | 3/3 | 1.0 / 1.0 | **1.0 / 1.0** | 0 / 0 / 0 | |
| `direct-pod-fault` (CPU stress on payment) | 3/3 | 1.0 / 1.0 | 0.67 / 0.67 | 0 / 0 / 0 | |
| `scheduled-recurring` (Schedule spawning CPU stress) | 3/3 | 1.0 / 1.0 | 0.33 / 0.33 | 0 / 0 / 0 | |
| `config-or-rollout` (broken image) | 3/3 | 1.0 / 1.0 | 0 / 0 | 0 / 0 / 0 | |
| `negative-control` (broken image + CPU-stress decoy) | 3/3 | 1.0 / 1.0 | 0 / 0 | 0 / 0 / 0 | decoy named 0 |
| `competing-causes` (packet loss + pod-kill) | 3/3 | **0.83** / 0.83 | 1.0 / 1.0 | 0 / 0 / 0 | group recall 1.0, cross-attribution 0 |

**The acceptance bar holds in every family**: 18 of 18 runs valid, no false strong authority, no false `RESOLVED`, the
decoy never named. Cause recall is 1.0 everywhere except `competing-causes`: in its repeat 2 (seed 88) the pod-kill
raised no lag above the threshold during the run (at most 29 messages; the loss had already cut the order traffic), the
lag alerts fired only at 07:10:30, after the run had ended, so no incident of the pod-kill's group existed and the
pod-kill could not be named; this is the world's outcome, counted as the frozen rule counts it.

Compared with `DEV` (variant A, three repeats each): cause and instance recall the same (1.0 where the symptom exists),
the bar held in both. Execution witnesses are **higher** on these variants (loss and CPU stress leave probe failures on
the target pod, which the observed-fault-execution rule can bind), and still absent for the rollout and the control,
where no strong rule exists yet (roadmap C5). The held-out set confirms the dev picture rather than contradicting it:
the engine names the cause and does not overclaim; strong evidence depends on the fault leaving pod-level failures.

## 13. Second HOLDOUT: pre-registration (2026-10-04, frozen before the run)

The engine changed after the first `HOLDOUT` (C1 wired, m21 §12.7; the trace read on by default, live-trace-design
§11), so by §12.2.6 it is measured once on a new `HOLDOUT` with new seeds. Fixed before any run:

1. **Scenarios:** the six variants B of §12.2.2, unchanged (each passed a blind phase 0, §12.7), with new seeds:
   `dependency-b` 89–91, `direct-b` 92–94, `scheduled-b` 95–97, `config-b` 98–100, `negative-b` 101–103,
   `competing-b` 104–106; 3 repeats each, 18 runs, suites `holdout2-<variant>`. No new phase 0: the seeds draw from
   the same parameter ranges.
2. **Engine frozen** at `1cb9d2b6` (`engine_commit` in every manifest), trace read on.
3. **Acceptance, the frozen bar:** no false strong authority, no false `RESOLVED`, at least 90% valid,
   `decoy_named` = 0 for the control.
4. **C1 confirmation (m21 §12.4.2–3):** `N` = 3, `F` = 3, `D` = 0.2 s are confirmed if no
   `FAULT_EXECUTION_EFFECT_AT_CALLER` witness names an actor off the world's chain (the decoy included) and no strong
   claim rests on one. Reported per family: witnesses on and off the chain, and the execution witness / effect link
   recall beside the first `HOLDOUT`. A violation is recorded as it is; nothing is tuned on this result.

### 13.1 Second HOLDOUT result (2026-10-04, engine frozen at `1cb9d2b6`, 18 runs)

Measured once, reported as it is (§12.2.6).

| Family (variant B) | Valid | Cause / instance | Execution witness / effect link | False strong / false `RESOLVED` / false elimination | Other |
|---|---|---|---|---|---|
| `dependency-fault` | 3/3 | 1.0 / 1.0 | 1.0 / 1.0 | 0 / 0 / 0 | |
| `direct-pod-fault` | 3/3 | 1.0 / 1.0 | 0.67 / 0.67 | 0 / 0 / 0 | |
| `scheduled-recurring` | 2/3 | 1.0 / 1.0 | 0 / 0 | 0 / 0 / 0 | seed 96: no alert fired |
| `config-or-rollout` | 3/3 | 1.0 / 1.0 | 0 / 0 | 0 / 0 / 0 | |
| `negative-control` | 3/3 | 1.0 / 1.0 | 0 / 0 | 0 / 0 / 0 | decoy named 0 |
| `competing-causes` | 3/3 | 0.83 / 0.83 | 1.0 / 1.0 | 0 / 0 / 0 | group recall 1.0, cross-attribution 0 |

**The acceptance bar holds**: 17 of 18 valid (94%), no false strong authority, no false `RESOLVED`, the decoy never
named.

**C1 is neither confirmed nor contradicted.** Its witness appears in none of the 262 stored diagnoses, on or off the
chain. The relation was barely exercised: the trace read failed in 15 of the 18 runs (`ConnectorReadError`, "Tempo
HTTP request failed"), wholly in four (`direct-b` 0 and 1, `config-b` 2, `negative-b` 0), where the sequential reads
of the `DEV` runs never failed. The lab's Tempo (CPU limit 500m) failed its liveness probe and restarted during the
run; the concurrent reads of live-trace-design §11 (up to 16 searches at once) are the likely load. The read is a
product change made with C1, so this is recorded as a finding of this `HOLDOUT`, not explained away; C1's
confirmation needs a new `HOLDOUT` after the read is fixed.

## 14. Third HOLDOUT: pre-registration (2026-10-04, frozen before the run)

After §13.1 the trace read is limited to two reads at once (live-trace-design §11). The second `HOLDOUT` is repeated
with new seeds; fixed before any run:

1. **Scenarios:** the six variants B unchanged; seeds `dependency-b` 107–109, `direct-b` 110–112, `scheduled-b`
   113–115, `config-b` 116–118, `negative-b` 119–121, `competing-b` 122–124; 3 repeats each, suites
   `holdout3-<variant>`.
2. **Engine frozen** at `58eadf38`, trace read on (the default).
3. **Acceptance:** the frozen bar, as §13.3.
4. **C1 confirmation:** as §13.4, and only if the relation was exercised: the trace read completed without a failed
   read in at least 90% of the valid runs. Otherwise C1 is again neither confirmed nor contradicted, and the read's
   failures are reported per run.

### 14.1 Third HOLDOUT result (2026-10-04, engine frozen at `58eadf38`, 18 runs)

Measured once, reported as it is (§12.2.6).

| Family (variant B) | Valid | Cause / instance | Execution witness / effect link | False strong / false `RESOLVED` / false elimination | Other |
|---|---|---|---|---|---|
| `dependency-fault` | 3/3 | 1.0 / 1.0 | 1.0 / 1.0 | 0 / 0 / 0 | |
| `direct-pod-fault` | 3/3 | 1.0 / 1.0 | 1.0 / 0.67 | 0 / 0 / 0 | |
| `scheduled-recurring` | 1/3 | 1.0 / 1.0 | 1.0 / 1.0 | 0 / 0 / 0 | seeds 113, 115: no alert fired |
| `config-or-rollout` | 3/3 | 1.0 / 1.0 | 0 / 0 | 0 / 0 / 0 | |
| `negative-control` | 3/3 | 1.0 / 1.0 | 0 / 0 | 0 / 0 / 0 | decoy named 0 |
| `competing-causes` | 3/3 | 0.83 / 0.83 | 1.0 / 1.0 | 0 / 0 / 0 | group recall 1.0, cross-attribution 0 |

**The acceptance bar does not hold on validity**: 16 of 18 runs valid (89%, under 90%); both invalid runs are
`scheduled-b`, whose alert did not fire (as seed 96 in §13.1), so the world raised no symptom. No false strong
authority, no false `RESOLVED`, the decoy never named.

**C1 is not confirmed; its condition of exercise fails.** Five `FAULT_EXECUTION_EFFECT_AT_CALLER` witnesses appear in
295 stored diagnoses (`direct-b` 1 and 2, `competing-b` 0), each naming the run's own cause, none off the chain. But
only 12 of the 16 valid runs read their traces without a failed read (75%, under the 90% of §14.4): `dependency-b` 2
(29 of 90), `config-b` 1 (4 of 30), `competing-b` 0 (46 of 90) and 1 (57 of 108). The lab's Tempo restarted three
times during the run (liveness probe, CPU limit 500m), also under the investigation's own pod-level searches. Both
findings are lab-side and are fixed before another `HOLDOUT`, never selected from this result.

## 15. Before the fourth HOLDOUT: two lab fixes (owner-approved 2026-10-04)

Both findings of §14.1 are lab-side; neither is chosen from an engine score (and no phase 0 diagnosis is read).

1. **Tempo:** the CPU limit goes from 500m to 2 (request 500m, memory 2 Gi as deployed), and the probes' timeout from
   1 s to 5 s, the liveness probe failing after six misses, not three (`infra/kubernetes/observability.yaml`). A
   3-minute load of 16 concurrent searches did not restart it.
2. **`scheduled-b`:** first raised to 56 to 72 workers, as `direct-b` in §12.4; its blind phase 0 failed (2 of 3
   runs valid, seeds 901 to 903), and the series showed why workers were not the cause: order latency already rose to
   2 to 3 s in every spawn, and `OrderRequestLatencyHigh` fired in Prometheus, but each 60 s spawn kept it firing for
   only 15 to 25 s (30 s ramp of the rate window plus the 15 s `for`), under the 30 s admission of
   connector-boundary §16, so no incident opened unless two spawns ran together. The amended variant keeps 20 to 28
   workers and spawns for **90 s every 120 s**, so each spawn's alert is active well past 30 s with a quiet gap
   between. Only validity decides it: a blind phase 0 of three runs in which the alert opens an incident every time.

The fourth `HOLDOUT` then follows §14 with new seeds (125–142, suites `holdout4-<variant>`), the engine frozen at the
same engine commit (the fixes are lab and harness, outside `packages/rca` and `apps/control_plane`).

**Blind phase 0 of the amended `scheduled-b` (2026-10-04):** 3 of 3 valid (seeds 904 to 906), each opening an incident
on `OrderRequestLatencyHigh` about 165 s after the cause; no diagnosis was read. The variant is accepted.

## 16. Fourth HOLDOUT: pre-registration (2026-10-04, frozen before the run)

As §14 in every point, with the fixes of §15: the six variants B (`scheduled-b` as amended), seeds `dependency-b`
125–127, `direct-b` 128–130, `scheduled-b` 131–133, `config-b` 134–136, `negative-b` 137–139, `competing-b` 140–142,
suites `holdout4-<variant>`; engine frozen at `58eadf38`; the frozen acceptance bar; C1 confirmed only if no
`FAULT_EXECUTION_EFFECT_AT_CALLER` witness names an actor off the chain, no strong claim rests on one, and at least
90% of the valid runs read their traces without a failed read.

### 16.1 Fourth HOLDOUT stopped (2026-10-04)

The owner stopped the run after its first two runs, both `INVALID` (`dependency-b`, seeds 125 and 126); no diagnosis or
score was read, and the lab was left clean (no chaos object, every pod running). Only the harness series were read:

- under 50 to 70% loss each probe sample takes 3 to 4 s, so the first 35 s hold 8 to 10 samples per probe, and the
  oracle's rule of three consecutive failures (contract §11) is reset by a single lucky success;
- seed 125: the target probe read `.xx.xxxxx`, its effect landed at +13.4 s, rounds after the client's propagation at
  +3.1 s (an inversion across rounds, contract §14.2.2); seed 126: the symptom probe read `.xx.xxx`, its symptom
  landed at +27.4 s, after the alert at +22.6 s;
- the earlier nine runs of this variant were valid because their sequences broke only after three failures; alert
  and propagation times are as in the earlier `HOLDOUT`s, so §15's lab fixes are not the cause.

## 17. Loss variants at 80 to 90% (owner-approved 2026-10-04)

Both loss variants (`dependency-b`, `competing-b`) draw their loss from 80 to 90% instead of 50 to 70%, so that a
success inside the first three samples becomes rare and the oracle's fields follow the world rather than chance. Only
validity decides: a blind phase 0 of three runs per variant (seeds 907 to 912), every run valid. Then a fifth
`HOLDOUT` replaces the stopped fourth, as §16 in every point, with seeds 143–160 (suites `holdout5-<variant>`), the
engine still frozen at `58eadf38`.

**Blind phase 0 at 80 to 90% (2026-10-04):** 6 of 6 valid (`dependency-b` seeds 907 to 909, `competing-b` 910 to 912);
no diagnosis was read. Both variants are accepted, and the fifth `HOLDOUT` is frozen as stated above.

### 17.1 Fifth HOLDOUT result (2026-10-04, engine frozen at `58eadf38`, 18 runs)

Measured once, reported as it is (§12.2.6). `scheduled-b` stopped before its first injection (the baseline probe of
`order-service` read only `URLError`, the port-forward of §12.4); its work directory was set aside as
`.refused-20261004T163932`, as the harness does for a refused baseline, and the suite was rerun unchanged.

| Family (variant B) | Valid | Cause / instance | Execution witness / effect link | False strong / false `RESOLVED` / false elimination | Other |
|---|---|---|---|---|---|
| `dependency-fault` (80–90% loss) | 3/3 | 1.0 / 1.0 | 0.33 / 0.33 | 0 / 0 / 0 | |
| `direct-pod-fault` | 3/3 | 1.0 / 1.0 | 1.0 / 0.67 | 0 / 0 / 0 | |
| `scheduled-recurring` (90 s every 120 s) | 3/3 | 1.0 / 1.0 | 0 / 0 | 0 / 0 / 0 | |
| `config-or-rollout` | 3/3 | 1.0 / 1.0 | 0 / 0 | 0 / 0 / 0 | |
| `negative-control` | 3/3 | 1.0 / 1.0 | 0 / 0 | 0 / 0 / 0 | decoy named 0 |
| `competing-causes` (80–90% loss) | 3/3 | **0.5** / 0.5 | 0.67 / 0.67 | 0 / 0 / 0 | group recall 1.0, cross-attribution 0 |

**The acceptance bar holds**: 18 of 18 valid, no false strong authority, no false `RESOLVED`, the decoy never named.

**C1 is confirmed** (m21 §12.4.3, §16): every one of the 18 valid runs read its traces without a failed read; eight
`FAULT_EXECUTION_EFFECT_AT_CALLER` witnesses appear in 274 stored diagnoses (`direct-b`), each naming the run's own
cause, none off the chain, so no strong claim rests on an off-chain one. `N` = 3, `F` = 3, `D` = 0.2 s stand.

Recorded beside it, not explained away: in `competing-causes` every symptom group was found but the second cause (the
pod-kill) was named in none of the three runs (0.5, against 0.83 in the first and third `HOLDOUT`s with the same
engine). The variant changed (80–90% loss, §17), not the engine; why the pod-kill is no longer named is open and is
looked at on `DEV` data, never selected from this result.

### 17.2 Why the pod-kill was not named (investigation, 2026-10-04)

Read on the three `competing-b` phase 0 runs at 80–90% (seeds 910 to 912, never a `HOLDOUT`) and, for the world and the
harness only, on the fifth `HOLDOUT`'s three runs. The engine named the pod-kill whenever an incident of its group was
collected: phase 0 seed 912 (`KafkaConsumerLag`, `OrderWorkerLagHigh` → `pod-kill-912`, `COMPETING`). Each miss has a
cause outside the engine's judgment:

| Run | Lag alert in Prometheus | Reached the control plane | Why the pod-kill was not named |
|---|---|---|---|
| phase 0 910, 911 | none | – | world: no lag (see below) |
| `HOLDOUT` 5 #1 (seed 159) | none | – | world: no lag |
| `HOLDOUT` 5 #2 (seed 160) | firing 15 s | no | shorter than the 30 s admission (connector-boundary §16) |
| `HOLDOUT` 5 #0 (seed 158) | firing 30 s (14:20:34 to 14:21:04) | late: the incidents opened at 14:24:49, by Alertmanager's webhook | at the edge of the 30 s admission, no poll admitted it; the harness had stopped collecting at 14:23:20, so the two lag incidents (diagnosed at 14:24:59, both naming `pod-kill-158`, `COMPETING`) fell outside the run |

**No lag:** the pod-kill falls 0 to 60 s into the loss, which lasts 120 to 160 s. At 80–90% loss the successful orders
drop from about 20 to about 0.6 per second, so almost nothing reaches Kafka and killing the consumer builds no lag above
the threshold; at 50–70% enough orders got through. Raising the loss for `dependency-b` (§17) starved the second
fault's symptom in `competing-b`, which shares the range.

So the 0.5 is the world, twice over: a fault with no symptom, and lag alerts at or under the 30 s admission (one of
them delivered by the webhook minutes later, after the run). A first reading of seed 158 blamed the harness's
collection window; the incidents' own creation time (14:24:49, after the harness's 14:23:20) corrects it. Nothing here is selected from the
`HOLDOUT`; any change is a harness or variant change, measured on a new `HOLDOUT`.

## 18. The competing variant's pod-kill near the end of the loss (owner-approved 2026-10-04)

From §17.2: the pod-kill's symptom needs orders flowing into Kafka. `competing-b` keeps 80–90% loss (§17) and its
pod-kill now lands **10 to 40 s before the loss is removed** (`second_before_end_seconds`, drawn after every other
parameter so a seed's other draws are unchanged), so the two causes still overlap and the consumer is down as the
orders return. The harness's collection window is unchanged: the late incident of §17.2 came from an alert at the
edge of the admission, which a longer window would not have caught. Only validity decides: a blind phase 0 of three
runs (seeds 913 to 915) in which every run is valid **and** a lag alert opens an incident. Then a sixth `HOLDOUT`,
as §16 in every point, with seeds 161–178 (suites `holdout6-<variant>`), the engine frozen at `58eadf38`.

**Blind phase 0, first round (seeds 913 to 915):** 3 of 3 valid; a lag incident in 913 and 914, not in 915. Its lag
alerts fired for 190 s and opened their incidents at 15:32:21, three seconds after the harness stopped collecting
(15:32:18): with the pod-kill near the loss's end, its symptom now arrives after the first cause is removed. So the
harness's collection does change after all (§17.2's first reading was right for this variant, though not for seed
158): when a second cause was injected, collection lasts at least until **180 s after it** (lag builds about a minute
after a pod-kill, then 30 s of admission, a poll and a diagnosis), as well as until 20 s without a new diagnosis. Other
families are unchanged. Second round: seeds 916 to 918, under the same condition.
