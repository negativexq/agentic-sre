# Frozen request binding adapter v1

2026-10-14. `packages/evals/frozen_request_binding.py` corroborates dependency log assertions against explicit frozen span records. It is an offline file adapter with no database/provider calls and no RCA authority. The engine remains 2.18.0. HOLDOUT records and decisions are unchanged.

## Corroborated relation

For each supplied dependency log, the adapter requires:

- Valid nonzero trace, client-span and parent-span IDs from structured log attributes.
- Exactly one matching CLIENT span and exactly one direct SERVER parent in the **same trace**. Duplicate candidates remain UNASSESSED; there is no first-match selection or search fallback by request ID.
- The expected caller service on all three observations and the configured peer host on the log and client span.
- The same nonempty request ID on the log and both spans; client HTTP method and error type consistent with the log; client ERROR status and parent HTTP 5xx.
- Timezone-aware, nested intervals: episode start ≤ server start ≤ client start ≤ log time ≤ client end ≤ server end ≤ episode end ≤ frozen cutoff. Missing end times, straddling operations, clock discrepancies and out-of-window logs remain UNASSESSED; no tolerance or clock adjustment is inferred.
- Distinct evidence IDs and source references, matching incident/run scope, and captures after the recorded event but no later than the frozen cutoff.

Passing these checks yields `request_relation=CORROBORATED`, with all three evidence IDs and source references. Failure yields UNASSESSED with concrete deficits. A missing trace is not evidence the operation did not happen. A successful parent means this failed-symptom relation cannot be established, not that the dependency is healthy.

The configured peer is a host assertion. The adapter does not map it to a selected target Pod or verify a Service declaration. The optional claim ID is report context only, not admission, support or membership authority.

## Trust and causal limits

The verification scope is **FROZEN_EXPORT_RECORD_CONSISTENCY**. Source authenticity and database manifest membership are explicitly NOT_VERIFIED. The wrapper's provenance is supplied export data, not a signed attestation. Comparing two frozen records provides corroboration, not independent proof that either source is complete or authentic.

The adapter never fabricates a target UID, mechanism fingerprint or fault-generation ID. Causal origin, cross-episode same-fault identity and recovery remain UNASSESSED. A failed child and failed parent sharing an observed request chain do not establish that the child caused the parent's failure. No output removes a historical rival, answers frontier, changes ranking/witnesses/digest, or creates RESOLVED.

The development [episode binding contract](episode-binding-evidence-contract.md) and this adapter remain separate: its endpoint recurrence checks cannot be populated from missing historical identities. The existing Episode Relevance v1 report is unchanged.

## Input and use

```sh
.venv/bin/python -m scripts.frozen_request_binding_analysis frozen-evidence.json \
  --output frozen-binding-report.json
```

Input is one `FrozenRequestEvidence` object or a list. It declares incident/run, explicit episode start/end, cutoff, caller service, configured peer, optional claim ID, logs and spans. Each record is wrapped with its incident/run, source reference and capture time. Logs use the existing `LogRecord`; spans use `TraceSpanObservation`. Maximum inventory is 10,000 of each per export. The input is validated before any output is written; output cannot overwrite input.

Request ID is now retained by the trace semantic-attribute allowlist as `http.request.header.x_request_id`, matching the source instrumentation. No rule consumes that field for RCA support. Older frozen spans lacking it stay UNASSESSED; old identities are not backfilled.

## Validation

A real local refused HTTP request is observed through server/client SDK spans and independently formatted logs, normalized through the existing trace attribute parser and Loki reader, then passed to this adapter. Its request relation corroborates; causal origin and recurrence stay UNASSESSED. Development controls include different-trace decoys with the same request label, wrong parents, duplicate spans, missing traces, mismatched peer/error/request, successful parents, old timestamps, wrong run and late/invalid provenance. Resolver/provider entry points are forbidden in the unit test.

This is a local instrumentation validation, not a new independent HOLDOUT or a claim of resolution gains. A later database exporter must validate exact frozen manifest/tape membership and record integrity before representing that provenance as verified. No database exporter or decision rule is included in this version.
