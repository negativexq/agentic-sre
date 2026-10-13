# Local HTTP DEV request-binding measurement — 2026-10-14

The new HTTP instrumentation and frozen request adapter corroborated 90 of 90 planned failed requests in the final local capture. This is instrumentation/link coverage under always-on in-memory SDK capture, not Kubernetes testbed, production collector/sampling coverage, root-cause accuracy or RESOLVED success.

## Protocol and scope

`scripts/measure_local_request_binding.py` runs isolated loopback HTTP order/payment services, using the repository's `TelemetryMiddleware` and `HttpPaymentGateway`. It makes 30 requests per scenario, with six concurrent driver workers. The driver records its own request IDs, timestamps and HTTP outcomes. Those 150 driver requests determine the denominator; it is not reduced to the subset with available logs or traces.

Protocol, expected failure types, instrumentation commit and runner hash are recorded before traffic. The runner source is copied into the artifact directory. No Kubernetes workload/control plane, shared collector, HOLDOUT database or stored diagnosis is changed. No RCA engine is evaluated. Source authenticity and database manifest membership are not verified by this local file capture.

Source facts are SDK spans and independently emitted JSON log records, normalized using the production attribute allowlist/parser. Capture is always-on in memory; this run does not exercise Loki/Tempo transport, retention, pagination, dropped exports or exporter backpressure.

## Final capture C

| Scenario | Driver requests | Dependency error logs | Corroborated failed-symptom relations | Outcome |
|---|---:|---:|---:|---|
| Connection refusal | 30 | 30 | 30 | ConnectionRefusedError; parent 500 |
| Timeout | 30 | 30 | 30 | TimeoutError; parent 500 |
| HTTP 503 | 30 | 30 | 30 | error.type=503; parent 500 |
| Handled HTTP 503 | 30 | 30 | 0 | Parent 201; all UNASSESSED with PARENT_5XX_NOT_RECORDED |
| Successful payment | 30 | 0 | 0 | Parent 201; no dependency error log |

Positive relation coverage: **90/90 (100%)** across the three planned failing-symptom scenarios. Every scenario produced its expected HTTP outcome and dependency failure type. Successful/handled requests are separate controls, not omitted failures. Current observed request relations do not establish causal origin, sole cause, historical fault continuity or recovery.

## Controlled evidence ablations

Each control is derived separately from the same 90 positive captured requests, without modifying the base export. These 540 assessments are not 540 independent fault experiments.

| Control | Assessments | False corroborations |
|---|---:|---:|
| Missing parent span | 90 | 0 |
| Wrong request ID | 90 | 0 |
| Duplicate client span | 90 | 0 |
| Wrong configured peer | 90 | 0 |
| Different-trace decoy with same request labels | 90 | 0 |
| Capture after frozen cutoff | 90 | 0 |

This checks the narrow adapter's rejection behavior, not a general false-RESOLVED or false-exclusion rate. Neither is evaluated here.

## Pilot and repeat accounting

Pilot A accidentally used a bound but non-listening loopback port for refusal. On this macOS environment that produced TimeoutError, not ConnectionRefusedError. Its numeric request-link observations were retained, but the capture was rejected as evidence for the planned refusal mechanism. `review.json` records that limitation; artifacts were not overwritten.

Capture B used a closed port and explicit mechanism validation; all three positive families produced 30/30 links. Final capture C reran after adding source snapshot preservation and strict scenario/acceptance validation. It again produced 90/90 links and zero false corroborations across its 540 ablations. The table above uses C only.

## Artifacts and reproduction

Final capture: `.local/diagnostics/local-binding-dev-20261014-c/`.

- `protocol.json` and `runner.py`: pre-traffic inputs and source snapshot.
- `driver-ledger.json`: independent request denominator and HTTP outcomes.
- `frozen-evidence.json`: captured log/span facts and provenance wrappers.
- `binding-report.json`: per-observation results and deficits.
- `summary.json`: scenario counts, positive coverage and control results.

```sh
.venv/bin/python -m scripts.measure_local_request_binding \
  --output .local/diagnostics/local-binding-dev-new --repeats 30 --workers 6
```

Output must be a fresh directory. A scenario/type mismatch, incomplete positive coverage, false corroborated control or corroborated handled-failure request makes the CLI fail its acceptance gate. This is DEV measurement; it is not an independent HOLDOUT validation.

## Next measurement

Collect the same narrow relations through the real OTLP → Loki/Tempo → run-manifest pipeline in a separately isolated DEV namespace/environment. Measure both collection loss and link coverage with the driver's full request denominator. This local result is a reason to test that transport path, not grounds to grant a new exclusion/admission/RESOLVED rule.
