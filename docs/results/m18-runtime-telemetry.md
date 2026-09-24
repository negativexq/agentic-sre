# M18 Runtime Telemetry

## M18A — Runtime Evidence Substrate

### Result metadata

| Field | Value |
|---|---|
| Milestone / phase | M18A — Runtime Evidence Substrate |
| Date | 2026-09-24 |
| Contract | `m18a.v1`, frozen in [m18a-runtime-evidence-contract.md](../architecture/m18a-runtime-evidence-contract.md) |
| Implementation commit at live run | `dae22cdc3ff845d66d0147d8890eab6f733ead34`; the live-validator-only working diff was subsequently committed as `659ef84db9a5c09cd71b9035fb15a697979a871a` with the exact code exercised |
| Final validation HEAD | `659ef84db9a5c09cd71b9035fb15a697979a871a` |
| Environment | Existing Kind cluster `agentic-sre`; `sre-demo` and `observability` namespaces |
| API usage class | CLASS 0 |
| Provider/model calls | 0 |
| Scenario set | Three bounded live pillar fixtures; not a diagnosis-accuracy benchmark |

The full machine-readable run output was written locally to the ignored file
`.local/m18a/live-validation.json`. The reproducible command is
`make m18a-live-validate`. The runner performs five authorized bounded reads:
two traffic queries and one resource-pressure probe through Prometheus, one
Loki query, and one Tempo query. The command leaves the existing Kind stack
running and terminates its local port-forwards on exit.

### Live evidence paths

| Pillar | Capability / target | Bounded query | Typed outcome | Finding and provenance |
|---|---|---|---|---|
| Prometheus normal metric | `traffic` / `sre-demo/Service/order-service` | `prometheus.http_request_rate.v1`; 2026-09-24 04:06:44–04:08:05 UTC; limit 32 | `OBSERVED_NORMAL`, six real samples | No Finding is emitted for a normal observation, as required. Descriptor `sha256:01fd75b2cb98a735df2c6028f6bf18d154aa0792b11c4a4a7bd884e6471823a4`; six source observation IDs retained. |
| Prometheus abnormal metric | `traffic` / `sre-demo/Service/order-service` | `prometheus.http_request_rate.v1`; 2026-09-24 04:07:45–04:09:00 UTC; limit 32 | `OBSERVED_ABNORMAL`, six real samples | `TRAFFIC_INCREASE` on the same Service; rule `prometheus.traffic_increase_baseline.v1`; descriptor `sha256:a9b2075548f1aa7290c5416b1d70d5a370cfe6a1924252cd89c275aaa1def2f3`; source observation IDs retained. |
| Prometheus resource probe | `resource_pressure` / `sre-demo/Pod/order-service-74fb47b6b-pj6gr` | `prometheus.resource_pressure.v1`; 2026-09-24 04:07:00–04:09:00 UTC; limit 32 | `NO_DATA`, zero samples | No Finding; no cAdvisor or kube-state-metrics resource series are scraped by the current Kind Prometheus. The empty resource query was not classified as normal. |
| Loki | `logs` / `sre-demo/Deployment/payment-service` | `loki.error_logs.v1`; 2026-09-24 04:08:33–04:09:03 UTC; limit 32 | `OBSERVED_ABNORMAL`, one record | `DEPENDENCY_ERRORS` on `sre-demo/Service/redis`; rule `loki.dependency_error_pattern.v1`; descriptor `sha256:9a92849b534569fa352e80c386e28e020869a81270837baa60169d4c28d9063f`; source observation ID retained. |
| Tempo | `runtime_traces` / `sre-demo/Deployment/payment-service` | `tempo.target_traceql.v1`; 2026-09-24 04:09:02–04:09:06 UTC; limit 32 | `OBSERVED_ABNORMAL`, six bounded spans returned | `DEPENDENCY_ERRORS` on `sre-demo/Deployment/payment-service`; rule `tempo.trace_observation.v1`; descriptor `sha256:c06452bdfe987a11c8d701f273e3a500db9097b580660d3e3fb420bd184df0f9`; six source IDs retained. The matching call fact is `order-service → payment-service`, `CLIENT_SERVER_SPANS`, with the callee outcome `ERROR`. |

The Loki and Tempo fixtures were emitted through the real OTLP Collector into
the deployed Loki and Tempo services, then read through the repository's
bounded adapters and normalizers. The Loki fixture was one explicit
`connection refused redis` log record. The Tempo fixture was one linked
CLIENT/SERVER span pair with an explicit non-success status and Kubernetes
resource attributes. These are telemetry fixtures, not claims that a live
service experienced those failures. Prometheus traffic came from read-only
GET requests for a fixed nonexistent order ID, which returned 404 and created
no application data.

All five semantic actions passed `validate_action` for their exact gap,
capability and canonical target. No native PromQL, LogQL or TraceQL was
provided by a caller; descriptors retain only trusted template IDs, bounded
parameters and stable identities. Findings carry the runtime pillar,
capability, target, requested/effective interval, query descriptor, source
observation IDs, and normalization rule. No root-cause, hypothesis-elimination,
confidence or resolution claim was made by this validation.

### Bounds and safety

| Control | Enforced limit / observed result |
|---|---|
| Prometheus query | Fixed semantic templates; timezone-aware window ≤ 3600 seconds; result limit ≤ 32; timeout 5 seconds; response cap 2 MiB |
| Loki query | Server-compiled service/error selector; timezone-aware window ≤ 3600 seconds; result limit ≤ 32; timeout 5 seconds; bounded response read |
| Tempo query | Trusted target-scoped TraceQL; timezone-aware window ≤ 3600 seconds; result limit ≤ 32; search ≤ 8 trace IDs; timeout 5 seconds; search/fetch response caps 1/4 MiB |
| Investigation budgets | Existing graph defaults: at most 8 tool calls and 2 calls per gap; action validation also checks exact authorization and capability-specific bounds. The live runner executed five fixed authorized reads. |
| Kubernetes writes | 0 |
| Application data writes | 0 |
| Secret access | 0 |
| Out-of-policy execution | 0 |
| Autonomous remediation | 0 |
| OTLP fixture records sent | 3 (one Loki log record and two Tempo spans) |
| Provider/model calls | 0 |

The normal metric demonstration for G18A.7 is the measured Prometheus request
rate, not a resource-pressure reading. The current Kind scrape configuration
does not expose cAdvisor/kube-state-metrics data, so the separate resource
probe correctly returned `NO_DATA`. This limitation remains explicit; the
probe is not presented as evidence of normal resource pressure. The G18A.7
gate is written for at least one observed-normal metric path and is met by the
measured traffic path. Unit tests also cover the typed resource-normalizer
contract, but they are not counted as a live resource proof.

### Validation executed

- `make m18a-live-validate` — PASS; all five semantic actions authorized and
  all three real backends returned typed observations. Prometheus abnormal,
  Loki and Tempo each produced a deterministic Finding.
- Focused runtime/investigation tests — 152 passed across runtime observation,
  Prometheus, Loki, Tempo, gap routing, authorization, candidate and graph
  suites.
- `make check` — PASS: Ruff, formatting, mypy (207 source files), and 848
  pytest tests passed. One pre-existing Starlette deprecation warning remains.
- `make precommit` — PASS.
- `make rbac-check` — PASS; the agent reader can list/watch approved resources,
  cannot read Secrets, patch Deployments or delete Pods.
- Source-only regression: the Task 6 truth-blind replay matched the sealed M15
  action audit for 25 scenarios / 150 actions with runtime telemetry
  unavailable. A follow-up regression test verifies an empty source-only
  snapshot does not advertise `runtime_traces`; `demo_source` retains its
  original action sequence.

### M18A hard gates

| Gate | Result | Evidence |
|---|---|---|
| G18A.1 | PASS | Semantic capability APIs and trusted backend query templates; no arbitrary query-language input. Focused reader/action tests. |
| G18A.2 | PASS | Server-side target, window, result, timeout and response bounds above; exact live actions passed `validate_action`; graph tool budgets remain bounded. |
| G18A.3 | PASS | Typed Prometheus traffic/resource, Loki log, Tempo span/call-fact observation schemas and shared provenance context. |
| G18A.4 | PASS | Live resource query returned `NO_DATA`; separate measured traffic query returned `OBSERVED_NORMAL`; unit tests assert they differ. |
| G18A.5 | PASS | Normalizers emit only typed Findings; tests cover RCA authority boundary. No M16 elimination or resolution code was added. |
| G18A.6 | PASS | Live Tempo caller/callee, client/server direction, error status and ordered durations. |
| G18A.7 | PASS | Real Prometheus low-rate `OBSERVED_NORMAL` and high-rate `OBSERVED_ABNORMAL`/`TRAFFIC_INCREASE` paths. Resource-pressure series specifically remain unavailable and `NO_DATA`. |
| G18A.8 | PASS | Real Loki read normalized the bounded dependency connection-refused pattern into `DEPENDENCY_ERRORS`. |
| G18A.9 | PASS | Each runtime Finding retains pillar, capability, target, requested/effective times, trusted descriptor, source IDs and rule ID. |
| G18A.10 | PASS | Kubernetes writes 0; application data writes 0; Secret access 0; out-of-policy execution 0. |
| G18A.11 | PASS | Focused tests, `make check`, `make precommit`, and `make rbac-check` passed. |
| G18A.12 | PASS | Real Prometheus, Loki and Tempo query → typed observation → deterministic Finding paths recorded above. |

### Limitations and disposition

- The live Kind Prometheus has no cAdvisor/kube-state-metrics resource series.
  Runtime resource reads remain `NO_DATA`; this is not interpreted as normal.
- The Loki and Tempo negative examples are explicitly labeled synthetic
  telemetry fixtures. They validate real ingestion/query/normalization paths,
  not incident prevalence or root-cause accuracy.
- No multi-service root-cause localization, alternative elimination, recovery,
  or `AMBIGUOUS → RESOLVED` claim is made. Those remain downstream M16/M17/M18B
  work.

M18A is COMPLETE with G18A.1–G18A.12 PASS. M18 remains IN_PROGRESS because
M16, M17 and M18B have not started. M15's historical G15.8 FAIL is unchanged.
