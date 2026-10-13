# HTTP dependency log correlation

2026-10-14, branch `fix/dependency-service-attribution`. Source instrumentation and additive observation storage only. Engine remains 2.18.0; RCA admission, ranking, witnesses, frontier and resolution do not consume the new attributes.

## Recorded chain

`TelemetryMiddleware` binds its inbound/generated request ID to a request-local ContextVar. The binding is reset on normal, exceptional and nested exits; concurrent request tasks remain isolated. `HttpPaymentGateway` propagates that existing ID as `x-request-id` and records it on the client span. It does not create a separate ID for an outgoing call.

When the Payment call fails, the error is emitted inside the client span with the captured request, trace, client-span and immediate parent-span IDs. The configured peer host, dependency name, HTTP method and error type are retained. The HTTP terminal error also carries its response status and method. These are source observations of a request relationship, not evidence that the child failure caused the parent failure.

The JSON formatter retains these explicit attributes, and uses the logging record's creation time instead of its formatting time. Loki extraction reads correlation from the complete bounded JSON body and/or OTLP structured metadata before truncating the legacy message text. Only allowlisted bounded values are retained; malformed/zero trace and span IDs, conflicting body/metadata assertions and oversized values are omitted. Missing structured data stays empty. Loki's timestamp continues to supply `LogRecord.at`; no event/arrival clock correction is inferred.

`LogRecord.attributes` preserves source assertions. `LogObservationRepository` stores and restores them through the same records-by-ID path used by frozen manifests; provider tapes serialize them through the model. Empty attributes are omitted from model serialization, preserving old log response shapes. Empty-attribute dedup keys retain their old shape; nonempty attributes participate in deduplication so a conflicting assertion cannot silently replace a record.

## Storage migration

`0033_log_correlation_attributes` adds nullable JSON `log_observations.attributes`. Existing rows are left NULL; no historical identities are backfilled. Apply the normal database migration before using this code with an existing database. A database still at 0032 does not have the column expected by the updated ORM.

No existing HOLDOUT database was migrated, replayed or rewritten for this delivery. Its frozen reports stay historical. Migration validation uses isolated test tables/databases; this change does not deploy workloads or alter a running experiment.

## Limits

- A connection refusal does not identify the selected target Pod. No target UID is inferred from Service selection or ownership.
- No mechanism fingerprint or fault-generation identity is fabricated. Cross-episode same-fault evidence is still unavailable from these logs alone.
- Parent-span ID is the immediate parent; nested and async paths need an observed chain. Request IDs alone are not globally unique proof; trace/span relationships must also be verified against source records.
- Raw request IDs remain application correlation assertions; no source authenticity verification is added.
- Kafka callback correlation, message identity, delivery/consumer binding and Redis instrumentation are outside this HTTP delivery.
- Sampled traces and correlated logs do not provide completeness, global health, recovery, exclusion or new RESOLVED authority.

## Validation and next step

Tests cover real refused HTTP calls inside server/client spans, repeated and concurrent requests, context cleanup, JSON formatting, Loki normalization, storage round trips, old observation serialization, schema upgrade/downgrade and unchanged RCA documents when attributes are added. Structured HTTP refusal, timeout and status-code tests remain intact.

Next, verify frozen log IDs against recorded trace/server facts in a read-only adapter for the development [episode binding contract](episode-binding-evidence-contract.md). Require explicit identity, timestamp and provenance checks; do not accept bare schema assertions as authenticated relations. This instrumentation cannot retroactively add the missing IDs to the three HOLDOUT23 diagnoses.

The read-only file adapter is now implemented; see [frozen-request-binding-adapter.md](frozen-request-binding-adapter.md). It corroborates exported log/span records and reports missing links, while source authenticity and database manifest membership remain explicitly unverified.

Real collector measurement found that this Loki setup returns OTLP assertions in stream labels. The allowlisted extraction now reads those labels as a third source and rejects conflicts with body/entry metadata. Results and the capture-manifest round trip are recorded in [the real OTLP DEV measurement](../results/2026-10-14-otlp-request-binding-dev.md).
