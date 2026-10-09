# Measured performance

Every published measurement, with its scope and limits: ITBench-Lite (historical and the last measured engine), the live suite, the M19 product path, the instrumented testbed, the Kind lifecycle validation and the benchmark methodology. Moved from the README, unchanged; the README keeps the summary.

## Measured root-cause performance

The frozen architecture of the time was evaluated against ITBench-Lite ground
truth with exact canonical entity comparison. TEST25 was held out and blind
when that architecture was frozen, and DEV10 is shown separately so that
development evidence is not confused with holdout evidence.

**Status of these numbers (2026-09-28).** Work on the causal semantics after
that freeze was done with all 35 scenarios in view, so the whole set is now a
**development regression set**: it protects against regressions and is not a
generalization estimate. New causal rules are not derived from it. The held-out
measurement comes from our own testbed, where the truth of every run is recorded
by the harness rather than published by a third party (see
[Instrumented testbed](#instrumented-testbed)).

Four of the 35 published labels cannot be matched by any prediction: their
entity filters match nothing in the scenario's own snapshot (Scenario-29, 23,
38, 105; Scenario-38's filter is not even a valid expression). Those four are
excluded from the accuracy denominator and reported separately.

| Evaluation | Exact root-cause accuracy | Raw | Model calls |
| --- | ---: | ---: | ---: |
| DEV10 development split | 9/9 (100%) | 9/10 | 0 |
| **TEST25 (blind when frozen)** | **[17/22 (77.3%)](../../evals/results/v1.1.2/README.md)** | 17/25 | **0** |
| All 35 combined | 26/31 (83.9%) | 26/35 | 0 |

The table above is **historical**: the frozen architecture of its time. The engine has changed since and is
measured separately. The last such measurement is engine 2.2.2 ([C11 analysis](../architecture/c11-itbench-equivalence.md); all 35
scenarios, full-source path, 31 scoreable); the current engine, 2.3.1, has not been re-measured on all 35 yet:

| Engine 2.2.2 (last measured) | Correct | What it measures |
| --- | ---: | --- |
| **Exact root cause (the ITBench score)** | **18/31 (58.1%)** | the answer equals the published label |
| Evidence-backed causal equivalence | 20/31 (64.5%) | the answer is a `Schedule` whose own spawned experiment (controller record, one UID) ran across the incident's onset |
| Structural controller record only | 26/31 (83.9%) | the answer's exact `Schedule` instance created the labelled experiment; says nothing about when it ran |
| Executing instance (engine witness) | 18/31 (58.1%) | the engine's own execution witness names the labelled object |

These measure different things. Only the first is the ITBench score; the others are reported beside it and never
replace it. Of the 13 exact misses, 2 are proven equivalences, 6 are possible equivalences only, 4 are answers the
published label cannot score, and 1 is a wrong causal actor.

Confidence against ground truth, for the historical architecture over all 35 scenarios
([v1.1.2](../../evals/results/v1.1.2/README.md)): `VERIFIED` predictions were 13/16 correct and `LIKELY` predictions 13/19.
The current engine's confidence has not been re-calibrated on these scenarios.

These accuracy figures are measured on the deterministic full-source path. The
bounded investigation path is measured separately, by how often it reaches the
same answer as the full-source path (21/25 on TEST25); its own ground-truth
accuracy has not been established.

On the frozen TEST25 run, every scenario used six validated physical reads:
150 reads across 25 incidents. The run produced 2,226 new evidence references and 244
normalized Finding emissions. The detailed [frozen benchmark report](../../evals/results/v1.1.2/README.md)
contains the dataset revision, manifest hash, prediction-freeze procedure, and
full aggregate metrics.

### Live scenario suite

ITBench-Lite grades root-cause accuracy on frozen snapshots. The internal live
scenario suite instead stages 25 real faults on a running Kubernetes cluster —
config, image, scale, NetworkPolicy, resource-starvation, deletion, crash, and
runtime-only faults — lets the real alerting path open an incident, and grades
the diagnosis the control plane actually stored.

| Tier | Scenarios | Correct |
| --- | ---: | ---: |
| DEV | 19 | 19 |
| **HOLDOUT** | **6** | **6** |
| **Total expected outcomes** | **25** | **25/25** |

The published 2026-09-24 live-suite re-anchor evaluated
`da6f0e671775e3ddba9374d3b2fd46e678478a56`. The frozen 25-scenario protocol was
later rerun during M16 validation at `b982c40`, preserving the same 25/25
expected outcomes and resolution distribution. Across that result, 16/16
root-cause actors and 9/9 abstentions were correct, with 0 fabricated
`RESOLVED` diagnoses. The resolution distribution was `RESOLVED`: 0,
`AMBIGUOUS`: 14, `INSUFFICIENT_EVIDENCE`: 11. Correct actor identification and
final epistemic resolution are distinct; the suite records the former even
when available evidence supports only an unresolved diagnosis. The result has
0 model calls. G16.7 remains historically `NOT MET`.

This separate in-house measurement uses labels with no external validity and
is not combined with ITBench-Lite. See the [methodology](../benchmarks/live-suite.md)
for scope and `make live-bench` to reproduce the protocol.

### M19 product-path validation

A later, bounded M19 validation exercised the revision path on a fresh,
dedicated Kind product cluster with real service traffic and a real
`OrderErrorRateHigh` alert:

```text
R1 INITIAL = AMBIGUOUS
→ product-created evidence requirement
→ R_early MANUAL = AMBIGUOUS
→ scheduler starts an EVIDENCE_DEADLINE revision
→ persisted A1 RECOVERED evidence eliminates a competing manifestation
→ R2 = RESOLVED
```

No synthetic diagnosis, hypothesis, or root state was injected. The scheduler
started the deadline revision; deterministic RCA rules made the diagnosis
decision. This is one bounded product-path validation, not a generalization or
accuracy benchmark and not a claim that the system resolves every incident.

## Instrumented testbed

The seen benchmarks cannot supply what a verified mechanism needs: the exact
execution, the first effect at the target, the propagation path and the
recovery. The testbed measures the engine against a world whose truth we record.

- **Lab:** a single-node Kind cluster (node image pinned by digest), Chaos Mesh
  2.8.4, the demo workload with Kafka and PostgreSQL, and an isolated control
  workload behind a default-deny NetworkPolicy. The control plane runs **outside**
  it with its own database; the Connector inside dials out, so recreating the lab
  keeps the diagnosis history.
- **Ground truth:** every run records a seven-field timeline (cause created,
  execution started, target effect, propagation, symptom, alert, recovery) whose
  fields have fixed producers (the injector or a separate oracle), plus the
  causal chain. Runs that contradict themselves are `INVALID`, never scored.
  The engine never sees any of it ([contract](../architecture/testbed-ground-truth-contract.md)).
- **Discipline:** a fresh control-plane database, a connector restart and a
  fresh target pod per run; manifests frozen with a hash, the engine version and
  the engine's git commit before any run (the runner refuses an engine that
  differs); write-once results; acceptance criteria fixed in advance
  (`false_resolved = 0`, `false_strong_authority = 0`, at least 90% valid runs,
  a decoy never named).
- **Held-out by construction:** variant A of every family is development data;
  variant B (a different injection) is the held-out set, run once with the
  engine frozen, after a blind phase 0 that checks only validity. A held-out
  result never selects a rule or a parameter.
- **Delivered in slices** ([design](../architecture/testbed-scenarios-design.md)),
  each validated by an unscored phase-0 run before its manifest is frozen.

**Results (engine 2.1.0).** Development is variant A of each family, held-out is variant B; three runs each.

| Family | Development: fault | Cause / instance | Held-out: fault | Cause / instance | Execution witness (dev / held-out) |
| --- | --- | ---: | --- | ---: | ---: |
| dependency fault | network delay on `payment-service` | 3/3 / 3/3 | packet loss on `payment-service` | 3/3 / 3/3 | 0/3 / 3/3 |
| direct pod fault | CPU stress on `order-service` | 3/3 / 3/3 | CPU stress on `payment-service` | 3/3 / 3/3 | 1/3 / 2/3 |
| scheduled recurring | `Schedule` spawning a delay | 3/3 / 3/3 | `Schedule` spawning CPU stress | 3/3 / 3/3 | 0/3 / 1/3 |
| configuration rollout | environment change rolled out | 3/3 / 2/3 | image change to a missing tag | 3/3 / 3/3 | 0/3 / 0/3 |
| negative control | a real change plus a decoy on an isolated workload | 3/3 / 3/3 | the same with a CPU-stress decoy | 3/3 / 3/3 | 0/3 / 0/3 |
| competing causes | two independent faults, scored per symptom group | 6/6 / 6/6 | packet loss plus a pod kill | 5/6 / 5/6 | 0/3 / 3/3 |

All 36 runs were valid, with 0 false strong authority, 0 false `RESOLVED` and the decoy never named. The one
held-out miss is a pod kill whose symptom never reached its alert threshold before the run ended, so no incident
of that group existed. Strong evidence depends on the fault leaving an execution witness at the exact target; the
rollout families had no strong rule at the time of this measurement; they have since
(roadmap C5a and C5b, each confirmed on a later held-out set). A run starts only from a quiet baseline: if the target
already holds a warning, the run is refused before anything is injected.

The service-level effect relation ([causal mechanism validation](../how-it-works.md#causal-mechanism-validation)) was wired into the engine after this measurement and confirmed on a later
held-out set with new seeds (18/18 valid, every trace read complete, eight witnesses, all naming the run's own cause).
Two earlier attempts could not decide it (the lab's Tempo failed reads under load; the scheduled variant's alert did
not always fire), both fixed in the lab. That same set showed the competing-causes runs no longer naming their second
cause (a pod kill); the cause was the variant, not the engine (the raised loss
starved the pod kill's symptom, and its late incident fell outside the run), and
a sixth held-out set with the variant fixed named both causes in every run, again
18/18 valid with no false strong authority. The
testbed has also found and fixed real defects: a rule that gave strong authority to an experiment that had ended
40 minutes before the incident, scorer flaws that overstated recall, harness isolation leaks, and short alerts that
could fall between two Alertmanager polls. These are small results, not a benchmark.

## Real Kubernetes validation

The Kind lifecycle validation exercises the product against a real cluster:

```text
healthy workload
  → injected Deployment rollout failure
  → Prometheus alert
  → Alertmanager incident
  → persisted observations and RCA
  → proposed rollback
  → A → B → A object journal
  → stable diagnosis from persisted state
```

This validates the incident path, observation persistence, and safety boundary.
M19 adds persisted run evidence manifests and ordered provider-read tapes,
persisted before results are consumed, plus an offline replay source and
deterministic replay with manifest, tape, and epistemic digest verification.
Replay reproduces the persisted epistemic universe; it does not make evidence
durable beyond the database and retention lifetime. The broader
[live scenario suite](../benchmarks/live-suite.md) — 25 staged faults,
graded end to end — is summarised under
[Measured root-cause performance](#measured-root-cause-performance) above.

## Benchmark methodology and reproducibility

The benchmark uses the pinned ITBench-Lite revision
`d0916b08ba421ce5e672e9ad68aa947d938dfef0` and manifest SHA256
`08a5e56dbfa604c59eed8282683d7b3ec224cd7db9303f90618dafd436423eac`.

DEV10 is the development split used while building the frozen architecture.
TEST25 was held out until that architecture was frozen, and no production code
was changed after its results were visible. Two different measurements were
made, by different runs, and they answer different questions:

- **Exact root-cause accuracy** (17/22 on TEST25) compares the deterministic
  **full-source** diagnosis with the scenario's published ITBench-Lite
  ground-truth entity, using the repository's alias-aware grader. Only an exact
  canonical match counts; a same-workload or nearby entity does not.
- **FULL_SOURCE agreement** (21/25 on TEST25) compares the **bounded**
  prediction with the same engine's own full-source diagnosis. Its blindness
  procedure: all 25 bounded predictions were persisted and SHA256-hashed before
  any full-source diagnosis was evaluated. Agreement measures information loss
  under a bounded read budget, not correctness; the bounded path's own
  ground-truth accuracy has not been established.

Both runs used the deterministic policy and zero model calls.

The [benchmark report](../../evals/results/v1.1.2/README.md) records the commit,
dataset identity, prediction artifact hash, frozen configuration, denominator,
and aggregate results. The [live-suite report](../../evals/results/live-suite-2026-09-24.md),
[M16 result](m16-positive-elimination.md), and
[methodology](../benchmarks/live-suite.md) provide live-run provenance and
protocol details.
