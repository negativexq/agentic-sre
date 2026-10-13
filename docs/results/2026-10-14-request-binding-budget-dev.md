# Real DEV request binding under bounded span capture

This measurement revisits three immutable real OTLP → collector → Loki/Tempo DEV captures (B, C, D). Each independent driver ran 150 requests: 30 refusal, 30 timeout, 30 HTTP 503, 30 handled HTTP 503 with a successful parent response, and 30 success. Each full remote export contains 300 spans and 120 dependency-error logs. Driver status, rather than the available traces, defines the 90 failed-request denominator.

## Results

The following results are identical in all three captures. Replicates share the same scenario sequence; they do not establish performance across randomized workloads.

| Span selection | Bound failed requests | Bound refusal requests |
| --- | --- | --- |
| Full export, 300 spans | 90 / 90 | 30 / 30 |
| Oldest 8 spans | 4 / 90 | 4 / 30 |
| Oldest 16 spans | 8 / 90 | 8 / 30 |
| Oldest 32 spans | 16 / 90 | 16 / 30 |
| Newest 8, 16, or 32 spans | 0 / 90 | 0 / 30 |

All profiles produced zero corroborated non-failed requests. The newest slices contain the later success traffic because this DEV driver runs scenarios sequentially. This is a workload-order effect, not evidence of recovery in an incident.

Slices are **counterfactual record caps**, not a replay of Tempo search ordering, provider target search, multiple investigation calls, or the complete production investigation budget. The production runtime-traces capability caps each response at 32 records. It does not follow that production can only collect 32 records in total. Existing evidence and multiple authorized queries may contribute additional spans.

Applying the existing exact Kubernetes target selector to the unmodified loopback DEV exports returns zero spans: these exports have no Kubernetes namespace/workload attributes. No resource identities were invented. This is an applicability limit of this capture, not a measured Kubernetes production selector failure.

## Historical Kubernetes DEV records

Read-only, repeatable-snapshot reads examined the latest three recorded runs from each of:

- `testbed_dev27_config_c_config_error_payment_0` (engine 2.17.0);
- `testbed_dev26b_negative_b_negative_image_decoy_0`.

All six exact base manifests matched their stored diagnosis membership digests. They contain respectively 1088/1061/1062 and 990/1024/990 TRACE members, but zero LOG members. Both schemas lack the newer log-attributes column. The frozen base manifests therefore cannot provide the request-linked logs needed for this accounting experiment. Investigation read-tape additions are outside this adapter and have **not** been assessed; zero base LOG members does not establish that the investigation observed no logs. All six recorded resolutions are AMBIGUOUS and were left unchanged. These are six selected runs, not a cohort-wide success estimate.

## Consequence for rollout accounting

The experimental rollout guard requires an existing strong mechanism witness and accounting of the target's complete dependency-log group, including uncited members. The real transport capture verifies request-link consistency, but contains no Kubernetes rollout case or strong witness. **The guard and RCA resolution were not evaluated on these captures. No real-data RESOLVED gain is claimed.**

Nevertheless, the record-cap experiment identifies a practical risk: full-group accounting must not assume a general trace sample contains every required client/parent pair. Promoting the current DEV guard unchanged could withhold shared explanations when valid evidence simply lies outside the selected sample. This result supports further collection design, not loosening the proof condition.

The next experiment should use a separate instrumented Kubernetes DEV failed-rollout case with frozen full-capture reference and the actual bounded investigation path. Measure separately (a) client/parent retrieval coverage, (b) existing strong-witness coverage, (c) aggregate-member accounting, and (d) paired final resolution. Test relation-targeted reads against the current approach before changing any resolver authority. Historical and competing-initiator controls remain necessary. HOLDOUT captures, shared workload configuration, production rules and stored diagnoses were not modified in this measurement.

## Reproduction

```sh
.venv/bin/python -m scripts.measure_frozen_binding_budget \
  --evidence .local/diagnostics/otlp-binding-dev-20261014-d/remote-evidence.json \
  --ledger .local/diagnostics/otlp-binding-dev-20261014-d/source-reference/driver-ledger.json \
  --output .local/diagnostics/binding-budget-dev-20261014/new-report.json
```

The CLI requires a fresh output path, records input SHA-256 hashes and does not overwrite inputs. Final D output: `.local/diagnostics/binding-budget-dev-20261014/report-v2.json`; B/C outputs: `replicate-b.json`, `replicate-c.json`. Historical read-only reports and summaries are in the same directory. The older `report.json` is an initial output whose completeness field was renamed in v2 to make clear that it counts driver refusal requests, not every aggregate log fact.
