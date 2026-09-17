# ITBench-Lite SRE — dev split

Mode `llm`, commit `8600d3be1563`, 10 scenarios, 136 model calls, 134.3 s.

| Metric | Value |
| --- | ---: |
| Macro F1 (all answers) | 0.700 |
| Macro F1 (verified answers only) | 0.600 |
| Answered | 100% |
| Root cause observable in snapshot | 90% |
| Root cause in top 5 | 90% |
| Root cause in top 3 | 90% |
| Root cause ranked first | 70% |
| Baseline: retired E10 agent (archive/experiments-2026-09) | 0.000 |

| Confidence | Answers | Correct |
| --- | ---: | ---: |
| LIKELY | 1 | 1 |
| UNVERIFIED | 3 | 0 |
| VERIFIED | 6 | 6 |

| Scenario | Prediction | Confidence | Correct | Rank | GT observable |
| --- | --- | --- | :---: | ---: | :---: |
| Scenario-4 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-7 | `otel-demo/Pod/otel-collector-564d9c7987-pnl25` | UNVERIFIED | no | 2 | yes |
| Scenario-8 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-11 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-12 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-17 | `otel-demo/Pod/recommendation-5f45f75855-tvqbl` | UNVERIFIED | no | 2 | yes |
| Scenario-21 | `chaos-mesh/StressChaos/otel-demo-valkey-memory-stress-v75mv` | VERIFIED | yes | 1 | yes |
| Scenario-29 | `otel-demo/Pod/otel-collector-564d9c7987-fvlzk` | UNVERIFIED | no | - | no |
| Scenario-34 | `otel-demo/Pod/valkey-cart-58df56c79c-bf4lr` | LIKELY | yes | 1 | yes |
| Scenario-91 | `chaos-mesh/NetworkChaos/otel-demo-fraud-detection-kafka-network-partition-mjvzp` | VERIFIED | yes | 1 | yes |
