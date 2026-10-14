# Exact log-ID request-pair retrieval: DEV implementation and measurement

The DEV collector now fetches the trace explicitly named by a dependency error log and retains only that log's exact client span and direct parent span. It does not search by request ID, repair missing IDs, infer resource identity, or supply new RCA authority.

`packages/evals/targeted_request_pairs.py` bounds each read to at most eight trace fetch attempts and 32 retained spans. Duplicate trace requests within a read are fetched once. Failed fetches consume budget. Missing, non-unique or conflicting pairs remain unassessed; a pair is never split to fit a record cap. Structural pair selection is followed by the existing frozen-request validator, which checks request identity, caller/peer, error type, parent 5xx, operation nesting, episode boundaries and provenance. Selection alone is not causal corroboration.

Live HTTP reads use `DevelopmentTransportProbe.trace_for_log` at the provider boundary. It requires the isolated DEV service-name pattern, an explicit nonzero 32-hex trace identity, and a zoned event window of at most one hour containing the log timestamp. Existing Tempo response-size and timeout bounds apply. This path is not registered as a production investigation capability, does not write the production read tape and does not modify resolver behavior.

## Measurement

Source logs are selected by their recorded caller, configured peer and `ConnectionRefusedError` attributes. The independent driver ledger is used only for scoring. Only raw Tempo responses supply spans; the SDK/in-memory reference and the complete parsed source-span list are not used to repair missing provider evidence.

| Profile | Bound refusal requests | Trace fetch attempts | Retained spans |
| --- | --- | --- | --- |
| Archived raw responses B | 30 / 30 | 30 | 60 |
| Archived raw responses C | 30 / 30 | 30 | 60 |
| Archived raw responses D | 30 / 30 | 30 | 60 |
| Live Tempo, D log identities, four reads | 30 / 30 | 30 | 60 |
| Live Tempo, D log identities, one read | 8 / 30 | 8 | 16 |

The four-read profile uses groups of 8, 8, 8 and 6 logs. Each group stays below its own bounds. The measurement CLI caps the total at four reads, so larger input sets leave unattempted logs explicit. In the one-read control, 22 source logs lie outside the read budget and remain unassessed. The denominator stays 30; unavailable pairs do not disappear from scoring. No corroborated request falls outside the driver's refusal set.

Live results are new HTTP reads of the same previously generated D requests, not a new independent workload or HOLDOUT. Archived B/C/D replicates use the same sequential scenario protocol.

Compared with the earlier counterfactual oldest-32-span slice (16 / 30 refusal requests), the four-read targeted path accounts for all 30. **The cost differs:** it fetches 30 traces and retains 60 spans rather than selecting 32 records. This does not establish a same-budget coverage improvement. It demonstrates that explicit log identities can retrieve the missing operations without increasing the per-read record cap. Four DEV helper invocations are not four already-authorized production tool actions, and the 30 HTTP requests are not four HTTP requests.

The first archived D run returned 26 / 30 because some raw response filenames used Tempo's unpadded trace-ID representation. Matching canonical 32-hex identities against zero-padded archive filenames restored 30 / 30; no log/span identity was changed. Live HTTP fetches use the explicit 32-hex ID directly.

## What remains

RCA and the rollout accounting guard have not been evaluated on these transport-only captures. No real-data RESOLVED increase, causal origin, global dependency health or completeness of production telemetry is claimed. Production resolver rules, engine version, stored diagnoses, workloads and HOLDOUT records are unchanged.

The next validation needs an instrumented Kubernetes DEV failed-rollout case with the existing strong mechanism witness and a complete reference capture. Compare actual bounded investigation strategies and their total HTTP/byte/latency costs, then check whether retrieved operations enter the witness and account for the dependency aggregate. Include historical, competing-initiator, identity-mismatch and partial-capture controls. Production integration must carry a recorded query descriptor, source-log references, read outcomes and replay semantics through the existing provider tape; this DEV probe does not replace that requirement.

## Reproduction

```sh
.venv/bin/python -m scripts.measure_targeted_request_pairs \
  --capture .local/diagnostics/otlp-binding-dev-20261014-d \
  --output .local/diagnostics/targeted-binding-dev-20261014/new-live-run \
  --tempo http://127.0.0.1:13200
```

Omit `--tempo` to use archived raw responses; add `--max-reads 1` for the bounded control. Outputs require a fresh directory. Protocol files record source hashes before reads; outputs preserve fetched raw responses, frozen evidence, corroboration report and cost summary. Final live artifacts: `.local/diagnostics/targeted-binding-dev-20261014/live-d-final/` and `live-d-one-read/`. Final archived outputs: `archive-b/`, `archive-c/`, `archive-d-v2/`.
