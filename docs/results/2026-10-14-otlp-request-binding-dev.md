# Real OTLP → Loki/Tempo → capture manifest DEV measurement

2026-10-14. Final capture C corroborated **90/90 failed-request relations** from records actually retrieved through the lab collector, Loki and Tempo. The capture survived the real manifest-builder/storage round trip unchanged. No RCA engine or RESOLVED rule was evaluated.

## What changed as a result of measuring

The first transport capture A delivered 210 error logs and 300 spans, but corroborated **0/90** relations. The log correlation attributes were present in Loki's stream labels. `LokiLogReader` previously read only JSON body/entry structured metadata, so its normalized records lost the IDs and failure/peer fields.

The reader now also accepts allowlisted source assertions from stream labels. Conflicting body, entry metadata and stream-label values are omitted; no source takes precedence. No identity is inferred from message text or topology. The fix adds explanatory source attributes, not a causal support, admission, exclusion or frontier rule. Engine remains 2.18.0.

After the fix, capture B corroborated 90/90. Final capture C repeated that result and automatically froze/reloaded the retrieved records through the production manifest builder. A/B/C remain separate artifacts; A was not overwritten or silently counted as successful.

## Measurement protocol

The harness drives real loopback HTTP order/payment calls under the repository's HTTP instrumentation and exports logs/spans over OTLP gRPC to the existing lab collector through a temporary supervised port-forward. Each capture uses a fresh service name, distinct from all shared workloads and HOLDOUT services. No workload, collector configuration, running control plane or HOLDOUT database is restarted or changed. The temporary forward was stopped after measurement.

An independent host driver makes 30 requests for each of five scenarios, with six worker threads. Its 150 request IDs/outcomes define the denominator. Local SDK/JSON capture is a **reference only**; no missing remote observation is repaired from it.

Remote traces are discovered by service-name search, not fetched from a local list of expected trace IDs. The explicitly bounded DEV search and log limits are 500, with a 120-second availability deadline. This does not measure the engine's narrower per-query search budget. Snapshots, search metrics, source hashes and API responses are retained. Missing/truncated/provider-failed data must not be presented as negative causal evidence.

## Final capture C results

| Scenario | Driver requests | Requests with remote dependency error log | Corroborated failed-symptom relation |
|---|---:|---:|---:|
| Connection refusal | 30 | 30 | 30 |
| Timeout | 30 | 30 | 30 |
| HTTP 503 | 30 | 30 | 30 |
| Handled HTTP 503, parent 201 | 30 | 30 | 0 |
| Successful payment, parent 201 | 30 | 0 | 0 |

All expected failure types and HTTP outcomes matched in the independent source-reference run. Remote positive coverage is **90/90 (100%)**. The handled-failure requests remain UNASSESSED with `PARENT_5XX_NOT_RECORDED`; no failed symptom is invented for a successful parent.

Tempo returned 150 discovered traces, 300 spans and a matching SERVER span for all 150 driver requests. Loki returned 210 error logs: 120 dependency failures plus 90 HTTP symptom error logs. All 120 expected dependency-failure requests had their remote log. Neither discovery/result limit was reached. Missing trace fetch count was zero in the final snapshot.

The first two C snapshots had the logs but no discoverable traces. The third snapshot, about 12 seconds after the first, had all traces. This is observed discovery availability under these polling intervals, not a latency percentile or a proof that an earlier empty response meant no operation occurred.

## Manifest round trip and authority

The 210 normalized logs and 300 spans were persisted to a **new local capture-only SQLite database**. The actual `build_manifest` created its base membership and EVIDENCE_GATHERED boundary; a query-only read reloaded exactly those members. Membership digest and full normalized record bodies matched; the same 90 relations corroborated after reload. Source references identify the actual manifest row IDs.

The label is `VERIFIED_CAPTURE_MANIFEST_ROUNDTRIP`, distinct from verification against a stored RCA diagnosis digest. No DiagnosisRow is fabricated, no hypothesis/support authority is created, and no RCA decision is generated. Tests explicitly assert that the capture database contains no diagnosis. Source authenticity is not cryptographically verified; root cause, historical same-fault continuity and recovery remain UNASSESSED.

## Artifacts and reproduction

Final capture: `.local/diagnostics/otlp-binding-dev-20261014-c/`.

- `transport-protocol.json`, `source-snapshot/`: pre-traffic bounds and source hashes/snapshots.
- `source-reference/driver-ledger.json`: full independent request denominator.
- `provider-reads/`: service searches, retrieved logs and trace payloads by poll.
- `remote-evidence.json`, `remote-binding-report.json`: provider-derived relations.
- `capture-manifest.sqlite`, `manifest-evidence.json`, `manifest-binding-report.json`: persisted member round trip.
- `summary.json`: collection, discovery and corroboration counts.

With the collector forwarded to local 14317 and Loki/Tempo available at 13100/13200:

```sh
.venv/bin/python -m scripts.measure_otlp_request_binding \
  --output .local/diagnostics/otlp-binding-dev-new --repeats 30 --wait-seconds 120
```

Output must be a new directory. Local reference capture remains always-on SDK; this run validates actual transport/normalization for the stated sample, not general sampling-loss behavior, a production guarantee, an independent HOLDOUT, false exclusion or false RESOLVED rate.

## Next decision gate

The real pipeline now retains enough current request identity for this narrow explanatory relation. That establishes neither the origin of the failure nor the end of an older fault. Before a historical Service rival can cease blocking RESOLVED, define the exact positive explanation/materiality rule and its scope; validate persistent/intermittent and genuinely competing faults separately, then freeze the rule before a new independent holdout. This measurement does not remove any existing rival or change any stored incident decision.
