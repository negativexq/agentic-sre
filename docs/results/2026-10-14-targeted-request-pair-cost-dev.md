# Live DEV request-pair acquisition cost

Three strategies queried the same real, previously generated DEV requests through the same local Tempo and an HTTP measurement proxy. Three rounds rotated strategy order. The independent ledger has 30 refusal requests; source logs select the caller, peer and refusal mechanism. Driver labels are used only for scoring. Full per-request HTTP measurements, raw provider responses and source hashes were captured.

## Result (three-round medians)

| Strategy | HTTP requests | Response body bytes | Strategy elapsed | Bound refusal requests |
| --- | --- | --- | --- | --- |
| DEV service-name search, limit 8, then fetch results | 9 | 36,525 | 151 ms | 2 / 30 |
| Exact log identities, one bounded read | 8 | 33,368 | 120 ms | 8 / 30 |
| Exact log identities, up to four bounded reads | 30 | 125,130 | 511 ms | 30 / 30 |

All HTTP responses were successful. The general search returned eight traces containing 16 spans; the 32-span return cap did not bind in this measurement. The one-read targeted profile retained 16 spans, and the four-read profile retained 60. Remaining requests in the partial profiles remain unassessed.

The full targeted strategy costs 3.33 times as many HTTP requests and 3.43 times as many downloaded body bytes as the general service search. Its median strategy elapsed time is 3.38 times higher here. In absolute terms that is +21 requests, +88,605 body bytes and approximately +360 ms. The one-read targeted strategy uses one fewer HTTP request and 3,157 fewer body bytes while corroborating eight instead of two relevant requests. These observations support targeting a known open relation before expanding a general sample; they do not establish that the first eight operations explain the whole symptom population.

Do not treat those latency ratios as stable production estimates: the local trace store is warm, samples are small and full-targeted elapsed varied from 303 ms to 1,203 ms. Strategy time includes parsing, corroboration and artifact writes. Separately summed upstream HTTP time medians are 129 ms (search), 83 ms (one read), and 399 ms (four reads). Loopback proxy startup/shutdown is outside the strategy timer. The proxy itself adds local overhead to client elapsed time.

Byte counts measure downloaded HTTP response bodies, before local JSON reserialization. They exclude headers, request bytes, TLS, storage/ingestion charges and provider billing. This is not a monetary cost estimate. Preexisting log acquisition is common input and excluded from all strategies; a production comparison must also account for any incremental log collection.

## Scope and next experiment

The search comparator uses the isolated DEV **service name** with limit 8. It does not claim equivalence to the production Kubernetes target query, complete investigation policy, retries, multi-target deduplication or read tape. The one-read/four-read labels are DEV helper bounds, not registered production tool actions. Limits in production remain unchanged. This experiment re-reads historical DEV traces and creates no new workload, RCA result or HOLDOUT result.

A read-only check of the current `sre-demo` Order image returned `request_correlation_module_present False` for `packages.telemetry.correlation`. No testbed/HOLDOUT runner process was found, but this does not independently prove the entire laboratory is free. Before an end-to-end rollout experiment, build the current branch under a separate image tag and use an isolated DEV environment; running the older image cannot validate the newly added request linkage. Existing workloads, control plane, events and stored diagnoses were not changed by this measurement.

For the Kubernetes DEV experiment, record matched full-reference and bounded-path cases and separately measure pair retrieval, strong-witness admission, aggregate accounting and final resolution. Keep total HTTP/byte/time costs visible. Prefer a single targeted read first; additional reads require explicit remaining evidence needs and must fit the eventual capability/policy budget. Never infer that all aggregate members were explained from a partial positive sample.

## Reproduction and artifacts

```sh
.venv/bin/python -m scripts.measure_request_pair_cost \
  --capture .local/diagnostics/otlp-binding-dev-20261014-d \
  --output .local/diagnostics/targeted-cost-dev-20261014/new-run
```

Final artifacts: `.local/diagnostics/targeted-cost-dev-20261014/b/summary.json`, per-round `http-cost.json`, preserved raw responses and corroboration reports. Initial run `a/` compared two strategies; `b/` adds the one-read profile and rotates all three. Outputs require fresh directories. The proxy binds only loopback, forwards only the bounded Tempo search/trace paths, and is stopped after each strategy.
