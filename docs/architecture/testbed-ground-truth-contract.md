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

### 4.3 Clock discipline

All stamps use one UTC clock source (the injector host). Oracle probe hosts record their measured
offset against it; a run whose offset exceeds 1 second is `INVALID`. Timestamps are recorded as
ISO-8601 with microseconds.

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
