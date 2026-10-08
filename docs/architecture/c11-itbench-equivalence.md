# C11: ITBench-Lite accuracy, exact and evidence-backed (2026-10-08)

Status: measured; roadmap C11 closed by this document. The official ITBench-Lite score is the **exact** track and is
unchanged by anything here. The other tracks are separate measurements of a different question and never replace it.

## 1. Problem

The engine answers 18 of the 31 scoreable ITBench-Lite scenarios exactly (engine 2.2.0 at `87f2535` and 2.2.2 at
`e53dbad`, full-source deterministic path, alias-aware grader). The published 26/31 belongs to an older architecture
(`f96073d`, ground-truth accuracy on the full-source path; `evals/results/v1.1.2`), so the two numbers are not one
engine measured twice. Of the 13 misses, 8 name a chaos `Schedule` where the label names an experiment of the kind the
`Schedule` spawns (owner-approved Schedule carrier, 2026-09-30). The question: in those 8, does the engine name the
actor that controlled the fault, or an upper object whose standing as the cause is unproven?

## 2. Semantics kept

- `root_cause` is the actor that controls or starts the fault; `executing_instances` are the objects the engine's
  fired execution witnesses name (m21 §17). Neither is substituted for the other.
- What ITBench receives is still `root_cause` (`agent_output`); ranking, closure, confidence and digests are untouched.
  The engine version does not move.

## 3. Evidence available in the snapshots

- **No chaos objects** are in the object snapshots (`k8s_objects_raw.tsv`), so no `ownerReferences` link an
  experiment to its `Schedule`. Only events exist.
- The controller's `Spawned` event of a `Schedule` instance (its events carry that instance's UID) names the child by
  **name** ("Create new object: <name>"); the child's own events carry its UID. In all 8 scenarios every spawned name
  has exactly one UID in the snapshot.
- The experiment's own events report execution: `Applied` ("Successfully apply chaos for <ns>/<pod>") and `Recovered`.
- Every one of the 8 snapshots holds **two incarnations** of the same `Schedule` name (two UIDs), one running about
  16:29–16:51 and one from about 17:52, while the incident onsets lie between 17:37 and 18:00.

## 4. Evidence levels and tracks

Relations are derived at prediction time from the snapshot alone (`packages/evals/itbench/equivalence.py`,
`controller_relations`), written into each sealed prediction file (`causal_relations`), and only read at grading:

| Track | Accepted as the one answer | Evidence level |
|---|---|---|
| `exact` | the root cause | the official ITBench match (existing grader) |
| `controller_record` | the root cause, or an experiment the root cause's **exact `Schedule` instance** named in a `Spawned` record, bound to one UID | structural: the controller created it |
| `controller_execution` | as above, and the experiment's controller reported applying it to a pod in an interval that reaches the onset (began at or before it, not recovered more than `W` = 5 min before it; m21 §11's `W`, reused) | execution, as reported by the controller |
| `executing_instance` | the root cause, or an instance named by the engine's fired execution witnesses | engine witness (m21 §17) |

Never a link: name similarity, the same namespace, closeness in time, the same pod, a second incarnation of a name,
a `Schedule` instance other than the root cause's, or anything read from the ground truth. Each track scores **one
answer**: an equivalence class matches at most one root group and counts as one prediction, so naming many
experiments adds nothing (precision is 1 or 0, recall is matched over the label's root groups).

## 5. Leakage controls

- Prediction never opens ground truth (the existing test that counts truth reads holds, and a new one checks the
  relations are sealed).
- Relations are covered by the prediction seal; editing them after sealing makes grading refuse (tested).
- Grading derives no candidate, relation or witness; a sealed run without relations reports the controller tracks as
  *not recorded* instead of computing them afterwards.

## 6. Method change during the measurement (stated, not hidden)

The first run (`all-2.2.2-c11-20261008T1648`, relations `c11.v1`) counted any controller-reported application before
the onset as execution evidence: 23/31. Reading its sealed relations showed that the root cause's `Schedule` instance
had last applied about an hour before the onset in Scenario-17, 21 and 35, i.e. an execution of an earlier fault
episode, which is one of the edge cases this evaluation must reject. The rule was tightened to an interval that reaches
the onset (`c11.v2`, `W` reused from §11, not chosen from these numbers) and the whole set re-predicted and re-graded.
Only the `c11.v2` run is reported as the result.

## 7. Result

Run `.local/runs/all-2.2.2-c11v2-20261008T1724`: engine 2.2.2, code at `9017760` (the tree was dirty only by
this document and the roadmap), 35 scenarios, 0 model calls, relations `c11.v2`, sealed before grading.

| Track | Correct (31 scoreable) | Accuracy | Macro F1 |
|---|---:|---:|---:|
| **`exact` (the ITBench score)** | **18** | **58.1%** | 0.581 |
| `controller_record` (structural) | 26 | 83.9% | 0.839 |
| `controller_execution` (execution across the onset) | 20 | 64.5% | 0.645 |
| `executing_instance` (engine witness) | 18 | 58.1% | 0.581 |

Macro F1 over all 35 answers (exact): 0.514. Scoreable: 31 (four labels match nothing in their own snapshot:
Scenario-23, 29, 38, 105).

The eight `Schedule` scenarios, every field from the sealed prediction (the label column is read at grading only):

| Scenario | Root cause (`Schedule`) | Label | Confidence / tier | Root support | Root instance UID | Spawned children (one UID each) | Applications of the root instance vs onset | `executing_instances` | Tracks rec / exec |
|---|---|---|---|---|---|---|---|---|---|
| 17 | `otel-demo-product-catalog-network-delay` | `chaos-mesh/NetworkChaos/.*product-catalog` | VERIFIED / SUPPORTED | change-onset-path FIRED; observed-fault-execution NOT_FIRED | `764b89e1` | 16 | applied 16:29–16:44 (onset 17:46:01); across the onset: 0, earlier episode: 16 | 0 | yes / no |
| 21 | `otel-demo-valkey-memory-stress` | `chaos-mesh/StressChaos/otel-demo-valkey-memory-stress` | LIKELY / SUPPORTED | change-onset-path FIRED; observed-fault-execution NOT_FIRED | `195db8a9` | 9 | applied 16:34–16:40 (2 of 9; the rest failed to apply) (onset 17:37:06); across the onset: 0, earlier episode: 2 | 0 | yes / no |
| 22 | `otel-demo-ad-memory-stress` | `chaos-mesh/StressChaos/otel-demo-ad-memory-stress` | LIKELY / SUPPORTED | change-onset-path FIRED; observed-fault-execution NOT_FIRED | `1bc9510a` | 8 | applied 17:53–17:59, **after** the onset (onset 17:39:55); across the onset: 0, earlier episode: 0 | 0 | yes / no |
| 35 | `otel-demo-ad-jvm-chaos` | `chaos-mesh/JVMChaos/otel-demo-ad-jvm-chaos` | VERIFIED / SUPPORTED | change-onset-path FIRED; observed-fault-execution NOT_FIRED | `d3c6933d` | 18 | applied 16:29–16:46 (onset 17:41:06); across the onset: 0, earlier episode: 18 | 0 | yes / no |
| 80 | `otel-demo-checkout-kafka-network-partition` | `chaos-mesh/NetworkChaos/otel-demo-checkout-kafka-network-partition` | VERIFIED / SUPPORTED | change-onset-path FIRED; observed-fault-execution NOT_FIRED | `e3f95fb7` | 9 | applied 17:54–18:02, **after** the onset (onset 17:41:31); across the onset: 0, earlier episode: 0 | 0 | yes / no |
| 81 | `otel-demo-shipping-quote-network-partition` | `chaos-mesh/NetworkChaos/otel-demo-shipping-quote-network-partition` | VERIFIED / SUPPORTED | change-onset-path FIRED; observed-fault-execution NOT_FIRED | `5a02288f` | 11 | applied 17:58–18:08 (onset 17:59:58); across the onset: 2, earlier episode: 0 | 0 | yes / yes |
| 83 | `otel-demo-email-checkout-network-partition` | `chaos-mesh/NetworkChaos/otel-demo-email-checkout-network-partition` | VERIFIED / SUPPORTED | change-onset-path FIRED; observed-fault-execution NOT_FIRED | `74f55774` | 8 | applied 17:57–18:04 (onset 17:57:10); across the onset: 1, earlier episode: 0 | 0 | yes / yes |
| 91 | `otel-demo-fraud-detection-kafka-network-partition` | `chaos-mesh/NetworkChaos/otel-demo-fraud-detection-kafka-network-partition` | VERIFIED / SUPPORTED | change-onset-path FIRED; observed-fault-execution NOT_FIRED | `f9227dd2` | 13 | applied 18:01–18:13, **after** the onset (onset 17:46:01); across the onset: 0, earlier episode: 0 | 0 | yes / no |

In every one the `Schedule` holds `SUPPORTED` through `change-onset-path`; none forms an execution witness. The
structural link exists in all 8 without the ground truth (the controller's `Spawned` records of the exact root
instance). The execution link reaches the onset in 2 (Scenario-81 and 83). In 3 the root instance applied only about
an hour before the onset (17, 21, 35: an earlier fault episode, recorded but not counted); in 3 it first applied 12 to
15 minutes after the onset (22, 80, 91), so it cannot have started the incident.

## 8. The 13 exact misses

| Class | Count | Scenarios |
|---|---:|---|
| Proven equivalence: the root instance's spawned experiment ran across the onset | **2** | 81, 83 |
| Possible equivalence only: structural link proven, no execution across the onset | **6** | 17, 21, 35 (root instance ran an hour earlier); 22, 80, 91 (root instance began after the onset, C18) |
| Label or data cannot score the answer | **4** | 20 (the label's pattern needs a suffix the Deployment's name lacks); 33 (label names the Pod kind, the change is on the Deployment; the same change was also made to `cart`); 102 (label names the Namespace, the answer is its ResourceQuota); 1 (label says more users on the load generator, the snapshot shows no such change; the only change is the `loadGeneratorFloodHomepage` flag in `flagd-config`) |
| Wrong causal actor | **1** | 34 (`cart` pod ahead of `valkey-cart` between equally scored, unsupported candidates, decided by name order) |

So of the 13 exact misses, 2 are fixed by proven equivalence, 6 hold only a possible equivalence, 4 cannot be
scored as answered, and 1 is a genuine RCA error. The `controller_record` 26/31 equals the historical 26/31 only by
coincidence of counts; it is a different measurement of a different engine and is not reported as a recovery.

## 9. Findings for the product (not benchmark adjustments)

1. **An execution that began after the onset is presented as the initiator (roadmap C18).** In Scenario-22, 80 and
   91 the engine's `Schedule` instance first applied 12 to 15 minutes after the onset. Its finding is still
   `INITIATING`, because `annotate_temporal_roles` allows `verification_onset_grace` (15 min) after the onset, and
   `m21.support.change-onset-path` supports it. The same pattern is reproduced on the own testbed without ITBench.
2. **The causal onset is not the actual symptom onset.** In 6 of the 8 scenarios diagnostic alerts began 3 to 14
   minutes before the alert coverage started; the onset of record is the first episode that began inside coverage,
   so the actual start is earlier and unknown. It is represented as one instant, not as uncertainty (C18).
3. **No execution witness forms on ITBench.** `m21.support.observed-fault-execution` did not fire in any of the 8
   (it needs an observed effect at the exact target), so `executing_instances` is empty there and the
   `executing_instance` track equals the exact one.
4. **Scenario-34** remains a ranking outcome between unsupported, equally scored candidates (C12's residue).

Even the earlier start does not overlap any observed `Schedule` application in those 6 scenarios: the first
incarnation stopped at about 16:51, the second began at about 17:52. What produced the symptoms around 17:30 is not in
the snapshot's events; this document does not guess.

## 10. Limits

- ITBench-Lite has been development data since 2026-09-28; none of these tracks is a generalization estimate.
- The controller link is the controller's own record naming the child; a re-created child with the same name would
  make it ambiguous (handled: not linked), and an event the snapshot dropped cannot be recovered.
- `controller_execution` checks the experiment ran across the onset, not that it caused the symptoms; only the
  engine's execution witness tests an effect.
- The historical 26/31 and the current 18/31 come from different engines; the difference is explained in roadmap C11,
  not by these tracks.
