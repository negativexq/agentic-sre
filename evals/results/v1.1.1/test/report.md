# ITBench-Lite SRE — test split

Mode `deterministic-v1.1.1-pre-agent`, commit `bcb365eafcd6`, 25 scenarios, 0 model calls, 0.0 s.

| Metric | Value |
| --- | ---: |
| Macro F1 (all answers) | 0.680 |
| Coverage-weighted VERIFIED F1 | 0.320 |
| VERIFIED conditional macro F1 | 0.800 |
| VERIFIED coverage | 40.0% |
| VERIFIED accuracy | 80.0% |
| Answered | 100% |
| Root cause observable in snapshot | 88% |
| Root cause in top 5 | 76% |
| Root cause in top 3 | 76% |
| Root cause ranked first | 68% |
| Baseline: retired E10 agent (archive/experiments-2026-09) | 0.000 |

| Confidence | Count | Correct | Precision | Share |
| --- | ---: | ---: | ---: | ---: |
| LIKELY | 15 | 9 | 60.0% | 60.0% |
| UNVERIFIED | 0 | 0 | 0.0% | 0.0% |
| VERIFIED | 10 | 8 | 80.0% | 40.0% |

| Scenario | Prediction | Confidence | Correct | Rank | GT observable |
| --- | --- | --- | :---: | ---: | :---: |
| Scenario-1 | `otel-demo/ConfigMap/flagd-config` | LIKELY | no | - | yes |
| Scenario-2 | `otel-demo/ConfigMap/flagd-config` | LIKELY | yes | 1 | yes |
| Scenario-5 | `otel-demo/ConfigMap/flagd-config` | LIKELY | yes | 1 | yes |
| Scenario-6 | `otel-demo/ConfigMap/flagd-config` | LIKELY | yes | 1 | yes |
| Scenario-9 | `otel-demo/ConfigMap/flagd-config` | LIKELY | yes | 1 | yes |
| Scenario-13 | `otel-demo/ConfigMap/flagd-config` | LIKELY | yes | 1 | yes |
| Scenario-14 | `otel-demo/ConfigMap/flagd-config` | VERIFIED | yes | 1 | yes |
| Scenario-15 | `otel-demo/ConfigMap/flagd-config` | LIKELY | yes | 1 | yes |
| Scenario-16 | `otel-demo/Deployment/shipping` | LIKELY | yes | 1 | yes |
| Scenario-18 | `chaos-mesh/PodChaos/otel-demo-product-catalog-network-delay-g5vwr` | VERIFIED | yes | 1 | yes |
| Scenario-19 | `chaos-mesh/NetworkChaos/otel-demo-email-checkout-network-partition-fntwp` | VERIFIED | yes | 1 | yes |
| Scenario-20 | `otel-demo/Deployment/product-catalog` | LIKELY | no | 2 | yes |
| Scenario-22 | `chaos-mesh/StressChaos/otel-demo-ad-memory-stress-6dgwf` | VERIFIED | yes | 1 | yes |
| Scenario-23 | `otel-demo/Deployment/checkout` | LIKELY | no | - | no |
| Scenario-24 | `otel-demo/Deployment/checkout` | LIKELY | yes | 1 | yes |
| Scenario-25 | `chaos-mesh/StressChaos/otel-demo-recommendation-cpu-stress-pfms4` | VERIFIED | no | - | yes |
| Scenario-31 | `otel-demo/NetworkPolicy/frontend-block-all-ports` | LIKELY | yes | 1 | yes |
| Scenario-33 | `otel-demo/Deployment/ad` | LIKELY | no | 3 | yes |
| Scenario-35 | `chaos-mesh/JVMChaos/otel-demo-ad-jvm-chaos-g98db` | VERIFIED | yes | 1 | yes |
| Scenario-38 | `otel-demo/HorizontalPodAutoscaler/kafka` | VERIFIED | no | - | no |
| Scenario-80 | `chaos-mesh/NetworkChaos/otel-demo-checkout-kafka-network-partition-g8m6w` | VERIFIED | yes | 1 | yes |
| Scenario-81 | `chaos-mesh/NetworkChaos/otel-demo-shipping-quote-network-partition-wg96t` | VERIFIED | yes | 1 | yes |
| Scenario-83 | `chaos-mesh/NetworkChaos/otel-demo-email-checkout-network-partition-dhs27` | VERIFIED | yes | 1 | yes |
| Scenario-102 | `otel-demo/ResourceQuota/otel-demo-memory` | LIKELY | no | - | yes |
| Scenario-105 | `otel-demo/Deployment/product-catalog` | LIKELY | no | - | no |
