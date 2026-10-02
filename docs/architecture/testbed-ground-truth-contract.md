# Testbed ground-truth contract (`testbed.v1`)

Status: **APPROVED** by the owner (2026-09-30), including the five decisions of §10 as recommended.
Nothing here is implemented yet. Roadmap step B1 (`roadmap.md`). Amendments are made here first.

## 1. Purpose

The seen scenarios cannot tell us whether the engine captured the *execution*, the first
*target-local effect*, the *propagation* to the symptom and the *recovery* of a known cause,
because their world was never instrumented. This contract fixes what an instrumented incident
records about the world so that those links can be scored. It extends
`packages/evals/live` (`LiveScenario`, `Tier`, actions, workloads, `grade`); it does not add a
second framework.

## 2. Invariants

1. **The injector writes the truth; the engine never reads it.** Ground truth is produced by the
   process that performs the injection, from its own journal and from an independent oracle
   probe. It is never placed in the engine's input, the Connector, the cluster, or any
   prediction (the rule already used for labels on the seen scenarios).
2. **Two observability tiers, reported separately.** *Production-realistic*: only what a
   customer's connector would supply (Kubernetes objects and events, Alertmanager, Loki,
   Prometheus, Tempo as configured). *Oracle*: synthetic probes and injector records used only
   for grading. The engine sees the first tier.
3. **Split and criteria are fixed before the first run.** Every scenario is `DEV` or `HOLDOUT`
   in the suite manifest; the engine version and the acceptance criteria are committed first.
   `HOLDOUT` results never select a rule or a parameter (the existing `Tier` semantics).
4. **Repetition is the unit of evidence.** A scenario family runs N times with seeded
   randomization; one run is an anecdote.
5. **A negative is true by construction.** A control states why no path exists and the
   construction is checked, not asserted.

## 3. Suite manifest (`testbed.suite.v1`)

One JSON document per suite, written before any run:

- `suite_id`, `engine_version`, `contract_version`, `created_at`, `salt`;
- per scenario: `scenario_id`, `family`, `tier`, `repeats`, `seeds`, and the randomized
  parameters with their ranges (target choice, injection offset, fault parameters);
- `acceptance`: the metric thresholds of §7, fixed here;
- `sha256` of the document, recorded in every run's output.

Families for the first suite: `direct-pod-fault`, `dependency-fault`, `scheduled-recurring`,
`config-or-rollout`, `negative-control`, `competing-causes`.

## 4. Timeline (`testbed.timeline.v1`)

One document per run. Each entry is a UTC instant with its **source** (`injector` or `oracle`)
and is nullable only where §4.2 allows.

| Field | Meaning | Source |
|---|---|---|
| `cause_created_at` | the API call that creates the cause (object or configuration) returned | injector journal |
| `execution_started_at` | the controller first acts on the target (for chaos: the first `Applied` on the target; for a rollout: the first new pod created) | injector observation of the controller |
| `target_effect_at` | first oracle-observed degradation at the target itself | oracle |
| `propagation_started_at` | first oracle-observed error at a downstream service across a known call edge | oracle |
| `symptom_started_at` | the alert condition first true, evaluated by the oracle on its own series | oracle |
| `alert_fired_at` | the alert's `startsAt` as Alertmanager reported it | injector observation |
| `recovery_at` | first instant all oracle probes are back to baseline after the cause is removed | oracle |

### 4.1 Ordering

`cause_created_at <= execution_started_at <= target_effect_at <= propagation_started_at <=
symptom_started_at <= alert_fired_at <= recovery_at` where each is present. `alert_fired_at -
symptom_started_at` is the alerting latency and is recorded, not corrected.

### 4.2 Nullable fields

`target_effect_at`, `propagation_started_at` and `symptom_started_at` are null only for a
`negative-control` (the fault has no effect path). Any other run with a null in these fields is
`INVALID`, not scored.

**Amendment (2026-09-30, owner-approved): `direct-pod-fault`.** In this family the faulted service is
also the one that shows the symptom, so there is no downstream service for the fault to propagate to.
`propagation_started_at` is null for a valid `direct-pod-fault` run (and only there, besides the
negative control), its chain has no `propagation` link, and the scorer does not measure
propagation-link recall for it. The other fields stay required. The oracle watches one probe of the
faulted service with two views: `target_effect_at` is the first sustained breach of the calibrated
threshold (§4.5) and `symptom_started_at` the first sustained breach of the alert's own threshold
(0.5 s). Because the alert threshold is the looser view of the same samples, the target effect cannot
be stamped after the symptom, and equal instants are allowed.

### 4.3 Clock discipline

All stamps use one UTC clock source (the injector host). Oracle probe hosts record their measured
offset against it; a run whose offset exceeds 1 second is `INVALID`. Timestamps are recorded as
ISO-8601 with microseconds.

### 4.5 Threshold calibration (amendment 2026-09-30, owner-approved)

The oracle's target and propagation thresholds are calibrated from the run's own quiet baseline and
written to the injector journal (`probe_calibration`) before the injection. The rule is **three times
the 90th percentile** of the baseline latencies, never below 0.1 s. It replaces three times the
maximum: a saturating fault has a heavy-tailed latency, and one stray baseline spike (a slow first
request) pushed a maximum-based threshold beyond the fault's reach (7.5 s against a 2 s effect). Runs
recorded before this amendment (suite `slice1`) used the maximum rule; their journals record the
values used.

### 4.4 Injector journal

Append-only JSON lines: every API call the injector makes (verb, object, response, instant, and
the resulting UID and resourceVersion). The timeline's injector-sourced fields are derived from it;
nothing is back-filled.

## 5. Causal chain

Alongside the timeline, each run records the ordered chain the world actually had:

```
links: [ { role, actor, instance_uid, mechanism, evidence_class } ... ]
```

- `role` is `cause`, `execution`, `target_effect`, `propagation` or `symptom`;
- `actor` is the canonical entity (`namespace/Kind/name`); `instance_uid` is set whenever the
  injector observed it (for chaos, the experiment and, for a Schedule, the schedule instance
  that spawned it) and is otherwise null with `knowable: false`;
- `mechanism` names the fault kind (never a claim about the engine's vocabulary);
- `evidence_class` is the class of observation the world produced for that link
  (`execution`, `effect`, `propagation`), so link-level recall is defined per class.

A `negative-control` carries `construction`: the checked reason no path exists (for example, no
call edge from the faulted service to any symptom service, verified against the oracle's own
traffic graph).

## 6. Expectations

Each scenario keeps the existing `RootCause` or `Abstain` expectation and adds `ground_truth`
(the timeline and chain of the run). `competing-causes` lists every true cause as separable
chains; a diagnosis that names one of them is scored per cause, not as a single label.

## 7. Scoring

Scored by the grader from the engine's stored diagnosis and the run's timeline and chain, never
fed back into the engine. Reported for `DEV` and `HOLDOUT` separately, per family, with the number
of valid runs:

- execution-witness recall (a strong or supported claim carries a witness on the true execution);
- effect-link recall, propagation-link recall (only where the chain contains that link);
- correct causal family; correct exact instance where `knowable`;
- **false strong authority** (any strong claim on an actor that is not on the chain);
- **false `RESOLVED`**; false elimination of a chain actor;
- abstention correctness on controls; time to resolution; evidence and read cost.

The seen-35 score is not comparable and is not reported beside these.

## 8. Storage

`.local/testbed/<suite_id>/manifest.json` and `.local/testbed/<suite_id>/<scenario>/<repeat>/`
containing `timeline.json`, `chain.json`, `journal.jsonl`, the oracle series, and the engine's
diagnosis. Files are written once and never edited; a re-run is a new repeat.

## 9. Out of scope

Choosing the fault parameters, building the oracle probes, the Chaos Mesh installation, and the
lab recreation (roadmap B0, B2, B4). The oracle is a separate process with no connection to the
engine, and this contract does not fix its implementation.

## 10. Decisions (approved 2026-09-30, all as recommended)

1. The seven timeline fields of §4, including `alert_fired_at` as a separate observation from
   `symptom_started_at`.
2. Runs with a missing required field or a clock offset above 1 second are `INVALID` and excluded
   from scores (§4.2, §4.3).
3. The chain records `instance_uid` with an explicit `knowable` flag (§5).
4. The manifest, including the split, the engine version and the acceptance thresholds, is
   committed before the first run and hashed into every run (§3).
5. Scoring is reported per tier and per family with valid-run counts, and is never merged with the
   seen-35 result (§7).

## 11. Implementation status (2026-09-30)

Implemented in `packages/evals/live/`: `ground_truth.py` (timeline, chain, suite manifest with a frozen
digest, run assembly with the `INVALID` rules of §4, write-once store), `journal.py` (the injector
journal and the derivation of the three injector fields from it; `Context.journal` and a `role`
argument journal every `kubectl` call), `oracle.py` (probes, the series writer, run-based derivation of
the four oracle fields, an NTP-style offset estimate) and `testbed_grader.py` (the §7 metrics from the
engine's `Diagnosis`, an aggregate per tier and family that skips links the world did not have and
never merges tiers). Scoring was exercised on the engine's real diagnosis of a scheduled chaos case.
Tests: 22 for the record, journal and oracle and 5 for scoring.

Two rules were made concrete and are stated here so that an amendment can change them:

- An oracle **effect** or **propagation** is the first run of three consecutive failed samples after the
  execution (or after the effect), and a **recovery** is the first instant every watched probe has three
  consecutive healthy samples after the cause is removed. A single blip is never an effect.
- Matching is by actor; a link's `evidence_class` decides which witness counts (an execution witness
  whose origin or holder is a chain execution actor; an effect witness whose symptom is the effect
  actor; a propagation witness whose path passes through the propagation actor). The engine emits no
  propagation witness yet, so that recall is 0 by construction until the service-level relation exists.

Not done, on purpose: the scenario definitions and their randomization (roadmap B4), the wiring that
makes `run_scenario` assemble and store a `RunRecord`, the per-scenario choice of probe roles, and any
live use. Nothing here has touched a cluster.

## 12. Measurement notes from phase 0 (2026-09-30)

Found by the first real run of slice 1; they refine §4 and are put here for the owner to confirm.

1. **Oracle instants are observation instants.** A probe result is stamped when the sample *ended* (its start
   plus its latency), not when it started. A sample that began just before a fault and was slowed by it can
   only finish after the fault began, so an effect stamped this way never precedes its cause; stamped at the
   start it could (and did, by fractions of a second).
2. **Event-derived instants have one-second resolution.** A Kubernetes event's timestamp is truncated to the
   second, so the controller's `Applied` instant can read up to a second before the create call that caused it
   returned (observed: 0.5 s). Within that resolution `execution_started_at` is set to the creation instant; an
   earlier reading is a real inconsistency and stays as observed, which invalidates the run (§4.1).
3. **Probes sample concurrently.** Sequential probing let one call slowed by the fault under test (a 3 s
   request) hold back the others, so the timeline showed the probes' coupling rather than the world.
4. **Isolation removes lingering events before anything restarts.** Kubernetes keeps events for about an hour,
   so an earlier run's chaos events reached the next run through the connector's first listing. They are deleted
   in the watched namespaces before the connector restarts and the control plane starts on its empty database.

## 13. Scoring amendment (2026-09-30, owner-approved)

Found by rescoring slice 1 (`testbed-scenarios-design.md`, "Slice 1 result"). In §7:

1. `execution_witness`, `effect_link` and `propagation_link` read only witnesses of the execution rules
   (`m21.support.observed-quota-rejection`, `m21.support.observed-fault-execution`). A structural path
   (`m21.support.change-onset-path`) is a D1 support, not an observation of an execution, an effect or a propagation.
2. `propagation_link` is not measured (`None`) while the chain's propagation link is not knowable; it enters
   with the service-level effect relation (roadmap C1).
3. The links of the chain belong to the whole fault: they are read from the witnesses of **every** incident the run
   stored. Naming, strong authority, elimination and timing still come from the primary incident (the earliest).

Rescoring writes `score.v2.json` beside the original `score.json`; nothing is re-run and no record changes.

## 14. Ordering and propagation amendment (2026-10-02, owner-approved)

Status: **APPROVED** by the owner (2026-10-02) and implemented (§14.5); recorded runs keep their files (§8).

### 14.1 What happened

All three repeats of slice 3 (`config-or-rollout`, suite `slice3`) are `INVALID` with "`symptom_started_at` precedes
`propagation_started_at`". In this family the target is probed directly (`POST /payments` on `payment-service`) and
the client probe (`order-service`) carries two views of the same samples, `propagation` (calibrated threshold, §4.5:
0.10 to 0.11 s here) and `symptom` (the alert's 0.5 s). The probes are sampled **concurrently** in rounds (§12.3) and
each sample is stamped when it ends (§12.1).

| Repeat | First failing round (start) | Target sample ends | Client sample ends | `propagation_started_at` |
|---|---|---|---|---|
| 0 | +11.3 s | +12.54 s (1.244 s) | +12.53 s (1.234 s) | +14.2 s (next round) |
| 1 | same shape | +12.6 s | +12.6 s, a few ms earlier | +14.5 s (next round) |
| 2 | +6.9 s | +7.84 s (0.934 s) | +6.92 s (HTTP 500 in 0.017 s) | +9.2 s (next round) |

Two measurement artefacts, not two worlds:

1. **Sub-round order is not observable.** Target and client fail in the same concurrent round; which sample ends
   first depends on each request's latency (in repeat 0 the client's slow call ended 10 ms before the target's), not
   on the order in which the world degraded. §4.1 then orders the two by noise.
2. **Propagation is searched only after the effect's end stamp** (`oracle_stamps`: `after=effect`), while the symptom
   is searched after the execution. A client failure in the effect's own round is therefore visible to the symptom
   view and invisible to the propagation view, although both views read the same sample. The propagation field skips
   to the next round and lands after the symptom of the same probe, which is impossible by construction (the
   propagation threshold is the looser view, as in the `direct-pod-fault` amendment of §4.2).

Repeat 2 also shows a real detail worth keeping in the record: the client's first failure was an HTTP 500 during the
rollout's pod replacement, faster than the target's first slow sample of the same round. A failed sample is a failure
under the oracle's existing rule (and an effect needs three consecutive failures, §11), so this changes no rule here.

### 14.2 Amendment to §4.1 (ordering)

1. Oracle instants are ordered by their **sampling round**: two oracle fields whose first samples belong to the same
   concurrent round are simultaneous for §4.1, whatever their end stamps. A round is one `sample_once` of every probe;
   its samples start within milliseconds of one another. The stamps themselves stay end-of-sample instants (§12.1) and are recorded
   unchanged.
2. An inversion across rounds stays a real inconsistency and invalidates the run, as before. Injector fields and the
   one-second event resolution of §12.2 are unchanged.
3. `propagation_started_at` is searched from the **execution** onwards, like `symptom_started_at`, and is then
   checked against `target_effect_at` by rule 1. The search no longer depends on the effect's end stamp.

This replaces the earlier wording "instants less than one second apart are simultaneous": a fixed second is both too
wide when rounds are fast and too narrow when slow samples stretch a round (rounds of 1.2 to 1.7 s were recorded in
slice 3); the round is what the oracle actually observes together.

### 14.3 Amendment to §4.2 (nullable propagation)

`propagation_started_at` is null **exactly when the scenario's chain declares no `propagation` link** (the fault
and the symptom are on the same service). The chain's shape is the family's, and the family is fixed in the frozen
manifest before the first run. This generalizes the
`direct-pod-fault` amendment so that later families (for example `scheduled-recurring` built on a CPU stress of the
symptom service) need no amendment of their own. A chain that declares a propagation link and a run with a null
propagation stays `INVALID`. The other required fields are unchanged.

### 14.4 Measurement before adoption

1. Re-derive the timelines of every recorded run (slices 1 to 3, phase 0 runs) offline from their stored series and
   journals with the amended rules, writing `timeline.v2.json` beside the original (§8, write-once); report every
   run whose validity or any oracle field changes, with the reason.
2. Expected, and to be checked rather than assumed: slice 3's three repeats become valid; no valid run of slices 1
   and 2 becomes invalid (the rules only remove a sub-round order); some `propagation_started_at` values move one
   round earlier.
3. Score the valid runs with the frozen manifest and the §13 scorer on the re-derived timelines (`score.v3.json`)
   against the acceptance thresholds (no false strong authority, no false `RESOLVED`).
4. No engine input changes; the engine never reads a timeline.

### 14.5 Implementation and measurement (2026-10-02)

Oracle stamps carry `round_at`, the start of their sampling round (`round_starts`: a stretch of the series in which no
probe repeats, as `Oracle.sample_once` writes it); `timeline_problems` orders two oracle stamps by round and every other
pair by instant, and takes the chain to decide whether propagation may be null; every oracle effect is searched from
the execution on. `packages.evals.live.testbed_lab rederive --suite <id>` re-derives stored runs.

| Suite | Before | After | Oracle fields moved |
|---|---|---|---|
| `slice1` (3 runs) | 3 valid | 3 valid | none |
| `slice2` (3 runs) | 3 valid | 3 valid | none |
| `slice3` (3 runs) | 0 valid (symptom before propagation) | 3 valid | `propagation_started_at` one round earlier: -1.66, -1.92, -2.28 s |

Slice 3 scored on the re-derived timelines (`score.v3.json`): cause named 3/3, instance named 2/3 (in repeat 1 no supported
hypothesis carries the cause's instance UID; not yet examined), execution witness 0/3 and effect link 0/3 (no rollout rule yet, roadmap C5;
expected), **false strong authority 0, false `RESOLVED` 0**: the frozen acceptance bar holds.

## 15. Competing causes: symptom groups and per-incident scoring (2026-10-03, owner-approved)

Status: **APPROVED** by the owner (2026-10-03) and implemented (`SymptomGroup`, `Chain.symptom_groups`, per-incident scoring in `testbed_grader.py`, scenario `competing-delay-podkill`).

### 15.1 Why

The `competing-causes` family (`testbed-scenarios-design.md` §4) injects a delay on `payment-service` and a pod-kill of
`order-worker` and assumes their symptoms are separable. A measurement on 2026-10-03 (unscored, nothing frozen) shows
they are separable only in one direction:

- **A pod-kill of `order-worker` alone** raised the lag of the alert expression (produced minus consumed over 2 minutes)
  from 0 to 42–114 for about 2 minutes, 4 to 11 times the threshold of 10; both lag alerts (`KafkaConsumerLag`,
  `OrderWorkerLagHigh`) fired about 50 s after the kill and lasted about 75 s. It cannot cause a payment latency.
- **A delay on `payment-service` alone** also moves the lag, weakly and intermittently, because `order-worker` calls
  `payment-service`: between −34 and +31 in slice 4 repeat 0 (crossing the threshold now and then, so a lag alert can
  fire), within ±9 in slice 4b repeat 0.
- No lag above 1 was seen during isolation or in four quiet minutes under load, so the pre-injection lag alerts of two
  phase 0 runs are not explained by isolation; their origin stays open.

A single cause per run, or one chain read from the primary incident (§7, §13.3), cannot score this: a lag incident has
two real contributors, and the run's incidents belong to different causes.

### 15.2 Symptom groups in the chain

The chain (`testbed.chain.v1`) gains an optional list of **symptom groups**, written by the harness from the scenario
definition before the run, never from what the engine reports:

```
symptom_groups: [ { alerts: [alert names], causes: [cause actors], required: [cause actors] } ... ]
```

- `alerts`: the alert names that make an incident belong to the group (an incident belongs to the group of its alert);
- `causes`: every cause actor that really contributes to those symptoms (the true cause set of the group);
- `required`: the subset that must be named for the group to count as found.

For the scenario of §4: a **latency group** (the latency alerts; causes and required: the payment delay) and a **lag
group** (`KafkaConsumerLag`, `OrderWorkerLagHigh`; causes: the pod-kill and the payment delay; required: the pod-kill).
`competing-causes` requires at least two cause links (unchanged) and, with this amendment, at least two groups.
An incident whose alert belongs to no group is reported and not scored.

### 15.3 Scoring per incident

For a chain with symptom groups, every stored incident of the run is scored against its own group, with the existing
meaning of *named* (an actor of a supported hypothesis, or `root_cause`):

- `group_found`: every `required` cause of the group is named;
- `cross_attribution`: a cause of the run that is **not** in the group's `causes` is named (for example the pod-kill
  named for a latency incident);
- false strong authority and false `RESOLVED` as in §7, per incident, against the group's `causes`.

Per run: `causes_named` counts a cause as named when it is found in at least one group that requires it (so neither
cause may be absorbed by the other); `cross_attribution` is the number of incidents with one. Reported per family with
the other metrics; the acceptance bar stays the frozen one (no false strong authority, no false `RESOLVED`), and
`cross_attribution` is reported without a threshold for this baseline.

### 15.4 Timeline

The timeline stays one per run and describes the cause the oracle probes (the payment delay: target, propagation and
symptom views as in `dependency-fault`). The pod-kill's instants (created, the pod's deletion, the new pod) are in the
injector journal and its chain links; they carry no oracle fields, and time-to-diagnosis is reported for the latency
group only. The second cause starts after a seeded offset of 0 to 60 s, so the two overlap.

### 15.5 Not changed

Families without symptom groups are scored exactly as today. The engine never reads the chain.
