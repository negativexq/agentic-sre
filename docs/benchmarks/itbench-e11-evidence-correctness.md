# E11 evidence correctness qualification

This is an offline prerequisite for the future E11 run. It repairs and tests the evidence layer before candidate ranking. No provider was constructed and no ground truth was read by runtime code.

## Corrections

- Metric observations are treated as `(timestamp, value)` pairs and sorted before baseline/incident windows, first/last values, deltas, slopes, and direction are calculated. Chronological, reversed, and shuffled inputs now produce the same result.
- Metric filters use exact structured identity fields, including `service_name` and `service.name`, with namespace separation. Similar names are not matched by substring.
- Workload specification summaries include `containers[].resources` and `initContainers[].resources`. Multiple container values remain attributed to their container rather than being collapsed into a misleading scalar.
- `LOG_ANALYSIS` is a bounded target-scoped semantic operation backed by structured log identities and normalized error-pattern summaries. Arbitrary log queries are not exposed.
- `COMPARE_REPLICAS` resolves sibling pods through owner references or explicit shared labels and reports unavailable when no peer exists.
- `RECENT_CHANGE_ANALYSIS` remains unavailable when the snapshot contains no trusted change history; current state is not represented as a recent change.

## Qualification

The focused evidence-correctness suite covers metric order invariance, exact service/namespace identity, container and init-container resources, bounded log evidence, owner-based sibling discovery, and singleton capability honesty. These tests are deterministic and provider-free.

The E11 retrieval layer remains ground-truth-free while building catalogs and rankings. Post-hoc retrieval metrics are computed only from frozen observable outputs.

`E11 LIVE BENCHMARK: NOT RUN`
