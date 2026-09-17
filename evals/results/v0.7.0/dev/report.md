# ITBench-Lite SRE — dev split

Mode `deterministic`, commit `8f1f6861ad17`, 10 scenarios, 0 model calls, 24.1 s.

| Metric | Value |
| --- | ---: |
| Macro F1 (all answers) | 0.900 |
| Macro F1 (verified answers only) | 0.800 |
| Answered | 100% |
| Root cause observable in snapshot | 90% |
| Root cause in top 5 | 90% |
| Root cause in top 3 | 90% |
| Root cause ranked first | 90% |
| Baseline: retired E10 agent (archive/experiments-2026-09) | 0.000 |

| Confidence | Answers | Correct |
| --- | ---: | ---: |
| LIKELY | 1 | 1 |
| VERIFIED | 9 | 8 |

| Scenario | Prediction | Confidence | Correct | Rank | GT observable |
| --- | --- | --- | :---: | ---: | :---: |
| Scenario-4 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-7 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-8 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-11 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-12 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-17 | `chaos-mesh/NetworkChaos/otel-demo-product-catalog-network-delay-b24p6` | VERIFIED | yes | 1 | yes |
| Scenario-21 | `chaos-mesh/StressChaos/otel-demo-valkey-memory-stress-zbhwg` | VERIFIED | yes | 1 | yes |
| Scenario-29 | `chaos-mesh/JVMChaos/otel-demo-ad-jvm-return-crqrb` | VERIFIED | no | - | no |
| Scenario-34 | `otel-demo/Pod/valkey-cart-58df56c79c-bf4lr` | LIKELY | yes | 1 | yes |
| Scenario-91 | `chaos-mesh/NetworkChaos/otel-demo-fraud-detection-kafka-network-partition-cx947` | VERIFIED | yes | 1 | yes |
