# v0.4.0 results

Commit `0eecbb95` (tag `v0.4.0`), clean tree. Each directory holds sealed
predictions, manifest, seal, and report.

| Run | Split | Scenarios | Macro F1 | Verified-only F1 | Root cause first | In top 3 | F1 where GT is observable | Model calls | Time |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| [Deterministic](test/report.md) | Test | 25 | **0.640** | 0.640 | 64% | 76% | 0.727 | 0 | 36 s |
| [LLM investigator](test-llm/report.md) (`gpt-5.6-luna`) | Test | 25 | **0.640** | 0.640 | 64% | 76% | 0.727 | 50 | 452 s |
| [Deterministic](dev/report.md) | Dev (tuned on) | 10 | 0.900 | 0.800 | 90% | 90% | 1.000 | 0 | 15 s |

Each test run was made once, from the tag. Scores are unchanged from
[v0.3.0](../v0.3.0/README.md); the release changes how chaos experiments are
timed and chosen (see [LLM dev runs](../llm-dev/README.md)), which picks
different, earlier experiment objects with the same correctness.

## What the LLM investigator did on the test split

- It agreed with the engine's first candidate in 23 of 25 scenarios, using 50
  calls in total (up to 7 in one scenario).
- Scenario-102: it chose `ResourceQuota/otel-demo-memory`, reasoning that the
  quota caused the ReplicaSet's `FailedCreate` events. The label names
  `Namespace/otel-demo`, so the local grader gives no credit, but the
  explanation is closer to the labelled cause than the engine's
  `ReplicaSet/ad-*`.
- Scenario-38: it replaced `HorizontalPodAutoscaler/ad` with an
  `otel-collector` pod because that pod's warnings started earlier. The label
  names an HPA (with an invalid filter), so this was a worse explanation that
  the grader could not score either way.
- Both replacements were between `UNVERIFIED` candidates, which the
  verification guard allows.
- Scenario-38 had two unusable replies that were retried. Both ended in
  `"}`, which suggests two JSON objects in one reply; the parser should
  accept the first object in a later release.

## Conclusion

On ITBench-Lite the investigator does not change the score. Its value shows
in explanations for unverified cases, where it can be better (Scenario-102)
or worse (Scenario-38). The engine's deterministic answer remains the
product default; the investigator stays opt-in.

Test misses and their causes are the same as in v0.3.0.
