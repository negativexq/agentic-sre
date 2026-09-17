# v0.5.0 results

Commit `a9e7a14c` (tag `v0.5.0`), clean tree, deterministic engine, no model
calls. Each directory holds sealed predictions, manifest, seal, and report.

| Run | Split | Scenarios | Macro F1 | Verified-only F1 | Root cause first | In top 3 | F1 where GT is observable | Time |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| [Deterministic](test/report.md) | Test | 25 | **0.680** | 0.640 | 68% | 80% | 0.773 | 61 s |
| [LLM investigator](test-llm/report.md) (`gpt-5.6-luna`) | Test | 25 | 0.640 | 0.640 | 64% | 80% | 0.727 | 397 s |
| [Deterministic](dev/report.md) | Dev (tuned on) | 10 | 0.900 | 0.800 | 90% | 90% | 1.000 | 25 s |

Each test run is from the tag. v0.4.0 scored 0.640 on the same split with and
without the investigator. The investigator run used 44 model calls.

The investigator run was started three times; only the third is valid and
published. The first had `SRE_LLM_ENABLED` unset and the second a total call
budget of 10, so the model was not called for 25 and 22 scenarios and those
answers were the engine's. The CLI now refuses to start in the first case and
the report flags the second. The published run was the first one with the
model reachable for every scenario; nothing was re-run for its score.

## What changed

New signals, all derived from cluster state without a model:

- **Quota rejections.** A `ResourceQuota` or `LimitRange` becomes a finding
  only when `FailedCreate` events show it rejecting pods (verified when the
  rejected workload is alerting), or when its `used` reaches `hard`. Quotas
  with headroom are no longer candidates.
- **Network policies that filter without denying everything**, scored below
  deny-all policies and never verified.
- **Container failures** from pod status: OOM kills, crash loops, image pull
  and container config errors, with matching fix proposals.
- **Resource pressure** from pod metrics: memory near its limit or CPU
  throttling that appeared around onset. Pressure that already existed before
  the incident is ignored; a first version without that baseline check
  lowered dev F1 to 0.80 (a throttled `cart` pod outranked the real cause in
  Scenario-34) and was not released.

**Disclosure.** Quota and partial network-policy handling were chosen after
reading the v0.4.0 test misses (Scenario-102 and Scenario-31). The rules are
general Kubernetes behavior and were checked on the dev split, but the test
gain on those two scenarios is not an independent measurement.

## Test changes against v0.4.0

| Scenario | v0.4.0 | v0.5.0 | Effect |
| --- | --- | --- | --- |
| Scenario-31 | no finding for the policy | `NetworkPolicy/frontend-block-all-ports`, `LIKELY` | now correct |
| Scenario-102 | `ReplicaSet/ad-*`, `UNVERIFIED` | `ResourceQuota/otel-demo-memory`, `VERIFIED` | still scored wrong: the label names `Namespace/otel-demo` |
| Scenario-38 | `HorizontalPodAutoscaler/ad`, `UNVERIFIED` | `Pod/kafka-*` (container errors), `LIKELY` | still wrong; the label's filter cannot match anything |

Scenario-102 is now a `VERIFIED` answer the grader counts as wrong, so
verified-only F1 stays at 0.640 (16 of 23 verified answers correct).

## What the LLM investigator did on the test split

- It agreed with the engine's first candidate in 23 of 25 scenarios, with no
  unusable replies and no failed calls (up to 5 calls in one scenario).
- Scenario-31: it replaced `NetworkPolicy/frontend-block-all-ports` (correct)
  with an OOM-killed `fraud-detection` pod, reasoning that the policy was
  created at 17:51, after the 17:27 alert onset, while the OOM kill was at
  17:22. Its timing argument is right; the engine keeps late changes as
  candidates within a grace window. Both candidates were `LIKELY`, so the
  guard allowed the swap. This cost the one point that separates the runs.
- Scenario-38: it chose a different `kafka` pod with an earlier container
  failure. The label cannot be matched either way.

On this split the investigator does not improve the engine; the engine
remains the default. Scenario-31 also shows that the label can name a change
recorded after the alerts started, which the engine's grace window accepts.

## Known issue in this release

The Scenario-31 finding reads "allows traffic only from listed peers". The
policy has `ingress: [{}]`, which in Kubernetes allows all ingress to the
selected pods. The summary text was fixed after the tag
(`fix(rca): describe network policy rules with Kubernetes allow semantics`);
the ranking and score do not change.

Other test misses and their causes are the same as in
[v0.4.0](../v0.4.0/README.md).
