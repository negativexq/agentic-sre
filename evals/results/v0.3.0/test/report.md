# ITBench-Lite SRE — test split

Mode `deterministic`, commit `1f3a6487172d`, 25 scenarios, 0 model calls, 35.6 s.

| Metric | Value |
| --- | ---: |
| Macro F1 (all answers) | 0.640 |
| Macro F1 (verified answers only) | 0.640 |
| Answered | 100% |
| Root cause observable in snapshot | 88% |
| Root cause in top 5 | 76% |
| Root cause in top 3 | 76% |
| Root cause ranked first | 64% |
| Baseline: retired E10 agent (archive/experiments-2026-09) | 0.000 |

| Confidence | Answers | Correct |
| --- | ---: | ---: |
| UNVERIFIED | 3 | 0 |
| VERIFIED | 22 | 16 |

| Scenario | Prediction | Confidence | Correct | Rank | GT observable |
| --- | --- | --- | :---: | ---: | :---: |
| Scenario-1 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | no | - | yes |
| Scenario-2 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-5 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-6 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-9 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-13 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-14 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-15 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-16 | `otel-demo/Deployment/shipping` | VERIFIED | yes | 1 | yes |
| Scenario-18 | `chaos-mesh/PodChaos/otel-demo-product-catalog-network-delay-gl228` | VERIFIED | yes | 1 | yes |
| Scenario-19 | `chaos-mesh/NetworkChaos/otel-demo-email-checkout-network-partition-5czjs` | VERIFIED | yes | 1 | yes |
| Scenario-20 | `otel-demo/Deployment/product-catalog` | VERIFIED | no | 2 | yes |
| Scenario-22 | `chaos-mesh/StressChaos/otel-demo-ad-memory-stress-w98sl` | VERIFIED | yes | 1 | yes |
| Scenario-23 | `otel-demo/Deployment/checkout` | VERIFIED | no | - | no |
| Scenario-24 | `otel-demo/Deployment/checkout` | VERIFIED | yes | 1 | yes |
| Scenario-25 | `chaos-mesh/StressChaos/otel-demo-recommendation-cpu-stress-flf8h` | VERIFIED | no | 2 | yes |
| Scenario-31 | `otel-demo/Pod/otel-collector-564d9c7987-6j4x2` | UNVERIFIED | no | - | yes |
| Scenario-33 | `otel-demo/Deployment/ad` | VERIFIED | no | 3 | yes |
| Scenario-35 | `chaos-mesh/JVMChaos/otel-demo-ad-jvm-chaos-452d7` | VERIFIED | yes | 1 | yes |
| Scenario-38 | `otel-demo/HorizontalPodAutoscaler/ad` | UNVERIFIED | no | - | no |
| Scenario-80 | `chaos-mesh/NetworkChaos/otel-demo-checkout-kafka-network-partition-dtxwx` | VERIFIED | yes | 1 | yes |
| Scenario-81 | `chaos-mesh/NetworkChaos/otel-demo-shipping-quote-network-partition-kx8th` | VERIFIED | yes | 1 | yes |
| Scenario-83 | `chaos-mesh/NetworkChaos/otel-demo-email-checkout-network-partition-4f5lv` | VERIFIED | yes | 1 | yes |
| Scenario-102 | `otel-demo/ReplicaSet/ad-554b849958` | UNVERIFIED | no | - | yes |
| Scenario-105 | `otel-demo/Deployment/product-catalog` | VERIFIED | no | - | no |
