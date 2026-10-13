# Manifest-scoped request evidence reader v1

2026-10-14. `packages/evals/manifest_request_evidence.py` reads exact frozen LOG/TRACE manifest members and passes them to the explanatory [frozen request binding adapter](frozen-request-binding-adapter.md). No provider, diagnosis recalculation, schema mutation or decision rule is invoked. Engine remains 2.18.0; historical run versions are preserved in report metadata.

## Source checks

- Require an explicitly read-only connection. PostgreSQL additionally requires REPEATABLE READ or SERIALIZABLE; the CLI opens a read-only REPEATABLE READ transaction. SQLite query-only mode is supported for isolated tests, without claiming PostgreSQL snapshot semantics.
- Require exactly one EVIDENCE_GATHERED boundary for the named run and exactly one stored diagnosis with a recorded manifest digest. Check their incident identity.
- Compute canonical membership digest across all source-type/ID entries and compare it with the diagnosis's recorded digest. Missing digest or mismatch is an integrity error, never an empty success.
- Fetch only the LOG/TRACE row IDs listed by that manifest. No current-row, newest-capture or time-window fallback. Missing members and malformed/oversized inventories fail explicitly. Other evidence types contribute to the membership digest but their contents are not loaded or validated by this reader.
- Verify each loaded row's incident, capture time and event index. Trace payload service/start must agree with its stored index; capture cannot precede the recorded event/end or exceed the frozen cutoff. Invalid input fails; it is not repaired with current data.

The report marks manifest membership `VERIFIED_AGAINST_RECORDED_DIGEST`. This is membership verification, not content authentication: the membership digest hashes source-type/row-ID pairs, not record bodies. Source authenticity remains NOT_VERIFIED; `record_integrity` describes scope/timestamp checks only. It is not a signed provenance chain or full RCA replay verification.

Episode start/end, caller service, configured peer and optional claim ID are explicit analysis arguments. The interval must fit inside the recorded run cutoff. They are retained in `requested_scope`; this reader does not certify that they match a claim's episode or a Service declaration. Claim ID remains report context. Only the corroborated request relation has explanatory output, with no admission, support, frontier, ranking or resolution authority.

## Legacy schema

The log table is reflected. If `attributes` is absent or NULL, attributes remain empty. Historical JSON/message text is never parsed to backfill identities. No migration is required to inspect these old runs. Current ORM application code still requires its normal migration when deployed; this reflected audit path does not change that requirement.

Only manifest evidence is exported. Investigation provider-tape additions not represented in that base manifest are outside this reader's scope. They require a separate bounded tape reader with its own digest and read provenance checks; they are not silently appended to base evidence.

## CLI

```sh
.venv/bin/python -m scripts.manifest_request_binding_analysis \
  --database testbed_database --run-id recorded-run-id \
  --episode-start 2026-10-13T15:00:00Z --episode-end 2026-10-13T15:03:00Z \
  --caller-service order-service --configured-peer payment-service \
  --output manifest-binding-report.json --evidence-output frozen-evidence.json
```

`--evidence-output` and `--claim-id` are optional. The report and evidence paths must be different. The exported evidence can be processed by the file adapter, but the file adapter correctly leaves database membership unverified: a JSON flag cannot grant database-verification authority.

## Recorded HOLDOUT23 audit

Three existing final OrderErrorRateHigh runs were read using read-only REPEATABLE READ transactions, without migrating or writing to their databases:

| Case | Recorded engine | Exact LOG members | Exact TRACE members | Membership digest | Request relations |
|---|---|---:|---:|---|---|
| negative-b-0 | 2.17.0 | 88 | 1274 | Matched | UNASSESSED |
| negative-b-1 | 2.17.0 | 34 | 1175 | Matched | UNASSESSED |
| config-b-2 | 2.17.0 | 102 | 1331 | Matched | UNASSESSED |

These 224 logs are the full LOG inventory, including HTTP symptom logs; the previously studied 112 dependency-error logs are a subset. All three databases lack the new attributes column. No positive binding was invented, and all three stored diagnoses remain AMBIGUOUS. This is source-boundary validation on historical cases, not independent validation of a new RCA rule or evidence of resolution gains.

Local artifacts: `.local/diagnostics/manifest-request-binding/summary.json` and each case's `.report.json` / `.evidence.json`.

## Validation and next gate

Tests include a corroboratable scoped record set, an unmanifested decoy sharing an evidence ID, a missing member, wrong recorded digest, wrong incident, late capture, inconsistent trace index, absent digest, and a legacy table without correlation attributes. A writable connection is rejected before evidence loading.

Next, collect new development requests under the instrumentation and export their manifest-scoped evidence. Validate actual binding coverage and gaps before any decision policy is proposed. Historical rival removal and RESOLVED authority remain separate work requiring a positive causal contract and a new independent holdout.
