# v0.3.0 results

Deterministic engine, no model calls, commit `1f3a6487` (tag `v0.3.0`), clean
tree. Each directory holds the sealed predictions, manifest, seal, and report
(`agentic-sre grade --out <dir>` re-grades them).

| Split | Scenarios | Macro F1 | Verified-only F1 | Root cause first | In top 3 | GT observable | F1 where GT is observable | Time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Test (reported) | 25 | **0.640** | 0.640 | 64% | 76% | 22 / 25 | 0.727 | 35.6 s |
| Dev (tuned on) | 10 | 0.900 | 0.800 | 90% | 90% | 9 / 10 | 1.000 | 14.6 s |
| Retired E10 agent, same 35 scenarios | 35 | 0.000 | — | — | — | — | — | — |

The test split was run once, from the tag. The dev split was used to tune the
rules, so its number is optimistic.

Confidence labels on the test split: 22 `VERIFIED` answers, 16 correct;
3 `UNVERIFIED` answers, 0 correct.

## Test misses

| Scenario | Prediction | Ground truth | Why |
| --- | --- | --- | --- |
| Scenario-23, 105 | `Deployment/checkout`, `Deployment/product-catalog` | Deployment filter `checkout-.*`, `product-catalog-.*` | The published filter needs a trailing dash, so no Deployment named `checkout` can match; the grader cannot award these. |
| Scenario-38 | `HorizontalPodAutoscaler/ad` | HPA filter `*.*` | The filter is not a valid regular expression and matches nothing. |
| Scenario-20 | `Deployment/product-catalog` (image change) | Deployment `product-catalog-.*` | Same filter issue for the top answer; the product-catalog pod, ranked 2nd, matched through an alias. |
| Scenario-25 | `StressChaos/...-cpu-stress-...` | `Schedule` `.*recommendation` | The engine names the running experiment; the label names its schedule (ranked 2nd). |
| Scenario-1 | `ConfigMap/flagd-config` | `Pod/load-generator-*` | A flag change outranked the load generator; its rollout restart ranked 2nd, but as the Deployment, while the label names the Pod. |
| Scenario-31 | `Pod/otel-collector-*` (warning events) | `NetworkPolicy/frontend-block-all-ports` | The policy produced no finding, so only warning events were left. |
| Scenario-33 | `Deployment/ad` (spec change) | `Pod/ad-*` | Right component, different kind; the ad pod ranked 3rd. |
| Scenario-102 | `ReplicaSet/ad-*` (warning events) | `Namespace/otel-demo` | A memory ResourceQuota in that namespace ranked 2nd; the engine never names a Namespace itself. |

Grading uses the published regular-expression filters and aliases
(`packages/evals/itbench/grader.py`); the official ITBench judge was not run.
