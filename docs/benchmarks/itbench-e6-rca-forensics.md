# ITB-E6 RCA forensic audit

Ground truth is used only after reading frozen prediction and trajectory artifacts.
The classifications below are post-hoc diagnostics and are not runtime behavior.

## Primary failure classes

| class | scenarios | percent |
|---|---:|---:|
| DIAGNOSIS_PARTIAL_OR_CORRECT | 1 | 2.86% |
| DIAGNOSIS_WRONG_CAUSE | 29 | 82.86% |
| MODEL_DECISION_INVALID | 2 | 5.71% |
| VOLUNTARY_STOP | 3 | 8.57% |

## Prediction-level details

| scenario | terminal | predicted | TP | P | R | F1 |
|---|---|---|---:|---:|---:|---:|
| Scenario-1 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/frontend-proxy | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-102 | SUBMIT_DIAGNOSIS | otel-demo/ResourceQuota/otel-demo-memory, otel-demo/Deployment/ad | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-105 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/product-catalog | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-11 | STOP | ∅ | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-12 | SUBMIT_DIAGNOSIS | otel-demo/Service/frontend-proxy, otel-demo/Deployment/frontend-proxy | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-13 | SUBMIT_DIAGNOSIS | _cluster/Pod/kube-controller-manager-i-036656695f1bb1e7f, _cluster/Pod/kube-scheduler-i-036656695f1bb1e7f | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-14 | MODEL_DECISION_INVALID | ∅ | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-15 | SUBMIT_DIAGNOSIS | otel-demo/Service/checkout | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-16 | SUBMIT_DIAGNOSIS | _cluster/ControlPlane/kube-scheduler, _cluster/ControlPlane/kube-controller-manager | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-17 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/frontend, otel-demo/Service/frontend, otel-demo/Service/checkout | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-18 | SUBMIT_DIAGNOSIS | otel-demo/Pod/checkout-5dccddf8bb-9dn2q | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-19 | SUBMIT_DIAGNOSIS | otel-demo/Service/checkout, otel-demo/Service/frontend | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-2 | SUBMIT_DIAGNOSIS | _cluster/Pod/kube-scheduler-i-0532e33054d94ef32, _cluster/Pod/kube-controller-manager-i-0532e33054d94ef32, otel-demo/Deployment/frontend | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-20 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/product-catalog | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-21 | SUBMIT_DIAGNOSIS | otel-demo/Pod/valkey-cart-58df56c79c-4cvft | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-22 | SUBMIT_DIAGNOSIS | otel-demo/Pod/ad-554b849958-l9ft5 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-23 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/checkout, otel-demo/Pod/checkout-d75b77f99-hgc68 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-24 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/checkout | 1 | 1.000 | 1.000 | 1.000 |
| Scenario-25 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/recommendation | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-29 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/frontend, otel-demo/Deployment/ad, otel-demo/Deployment/product-catalog | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-31 | SUBMIT_DIAGNOSIS | otel-demo/Pod/frontend-675fd7b5c5-8qrfh, otel-demo/Service/frontend-proxy, otel-demo/Deployment/frontend-proxy | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-33 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/frontend-proxy, otel-demo/Pod/frontend-675fd7b5c5-ljs28 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-34 | SUBMIT_DIAGNOSIS | _cluster/Component/kube-scheduler, _cluster/Component/kube-controller-manager | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-35 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/ad, otel-demo/Deployment/frontend | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-38 | SUBMIT_DIAGNOSIS | kube-system/Pod/kube-controller-manager-i-08942eb4ee52d7d33, kube-system/Pod/kube-scheduler-i-08942eb4ee52d7d33 | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-4 | SUBMIT_DIAGNOSIS | otel-demo/Deployment/frontend | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-5 | STOP | ∅ | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-6 | SUBMIT_DIAGNOSIS | _cluster/Node/i-01101b2dc9fb0033f, otel-demo/Deployment/frontend | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-7 | MODEL_DECISION_INVALID | ∅ | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-8 | SUBMIT_DIAGNOSIS | _cluster/Secret/unknown, otel-demo/Deployment/frontend | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-80 | SUBMIT_DIAGNOSIS | _cluster/Deployment/kube-scheduler, _cluster/Deployment/kube-controller-manager | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-81 | SUBMIT_DIAGNOSIS | otel-demo/Service/checkout, otel-demo/Service/frontend-proxy, otel-demo/Service/shipping, otel-demo/Service/frontend | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-83 | SUBMIT_DIAGNOSIS | otel-demo/Pod/frontend-675fd7b5c5-kzdpp | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-9 | STOP | ∅ | 0 | 0.000 | 0.000 | 0.000 |
| Scenario-91 | SUBMIT_DIAGNOSIS | _cluster/Pod/kube-controller-manager-i-08602542d82e8f6e6, _cluster/Pod/kube-scheduler-i-08602542d82e8f6e6 | 0 | 0.000 | 0.000 | 0.000 |
