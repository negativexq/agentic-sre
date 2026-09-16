# ITB-E10 baseline engineering observations

This note records only verified, immutable historical artifacts. It does not modify or reinterpret E10.

## E7 reachability baseline

The frozen E7 retrieval artifact reports 12 observable root groups out of 35. Recall was:

| metric | value |
|---|---:|
| Recall@1 | 0.229 |
| Recall@3 | 0.314 |
| Recall@5 | 0.343 |
| Recall@10 | 0.343 |
| conditional Recall@1, observable roots | 0.667 |
| conditional Recall@3, observable roots | 0.917 |
| conditional Recall@5, observable roots | 1.000 |

The bottleneck was representability/reachability, not only model selection.

## E10 execution baseline

The immutable E10 result is 35/35 scenarios, with 405 successful provider responses, 273 semantic actions, 16 `SUBMIT`, 19 `STOP`, zero protocol stalls, zero action rejections, and zero measured safety violations. Local fixed-35 grading was macro F1 = 0 and micro F1 = 0; TP = 0, predicted entities = 16, truth entities = 35, and diagnosis coverage = 16/35 (0.457).

The 16 submitted identities were concentrated in a small set: `prometheus/StatefulSet/prometheus-prometheus-kube-prometheus-prometheus` appeared 6 times, `otel-demo/Deployment/flagd` 5 times, Prometheus objects appeared 3 more times, and the remaining two were `otel-demo/Deployment/product-catalog` and `otel-demo/Deployment/ad`. This supports a generic-infrastructure concentration diagnosis, but is not itself a causal explanation.

Post-hoc E10.1 attribution is recorded separately in `itbench-e10-failure-attribution.json`. It uses GT only after loading the frozen traces and records provider calls as zero.

## Engineering implication

The E11 work separates an observable entity catalog from a bounded shortlist, derives ranking from structured incident evidence, and adds an observe-first control path. These are new offline qualification artifacts; E10 artifacts remain historical evidence.

`E11 LIVE BENCHMARK: NOT RUN`
