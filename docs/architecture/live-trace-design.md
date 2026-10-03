# Live traces in the base diagnosis (design, owner-approved 2026-10-03)

Status: **APPROVED** by the owner (2026-10-03) and implemented behind `SRE_TRACE_CAPTURE` (off by default until the
shadow measurement of §4 adopts it). Roadmap A6, prerequisite of C1, C3 and the effect side of C5.

## 1. Measured problem

The engine reads distributed traces through `source.trace_observations()` (`engine.py`, `prepare_case_inputs`), and
everything trace-based is derived from it: the runtime graph, the runtime evidence and its paired non-success call
facts. The live source returns nothing (`live.py`: "Live trace ingestion is not part of this source contract yet").
Traces enter a live diagnosis only when an investigation step asks for them through the provider boundary
(`provider_adapter.query_tempo`, `TempoTraceReader`); in the stored testbed diagnoses `runtime_traces` appears only as a
candidate tool of an unanswered frontier, never as evidence.

Consequences, all measured on the testbed: the service-level effect relation (m21 contract, C1) can never hold live,
because it is defined on trace edges; the asynchronous producer-to-consumer edge (C3) has no source; a rollout's effect
at the caller (C5, HOLDOUT `config-image-payment`: the new pod fails to pull, the alert is `OrderErrorRateHigh` at the
caller) cannot be linked. The only service-level observation present is log-derived `DEPENDENCY_ERRORS`, and only when
calls fail outright; delay and loss leave none.

## 2. What exists

- The Connector holds Tempo (`TEMPO_URL`) and serves `runtime_traces` through `query_tempo` (contract §10), with bounded
  responses and a completeness diagnostic (`TempoSearchCompleteness`).
- `TempoTraceReader` compiles a TraceQL query for one target entity over a time window and returns typed
  `TraceSpanObservation`s.
- Replay already feeds recorded spans to the engine (`replay.py`), and the ITBench path exercises the whole trace
  pipeline.

## 3. Proposal

1. **Base read at diagnosis.** Before the engine runs, the control plane reads spans from Tempo through the Connector
   for the evidence window the engine already uses for the incident (to be read from the case context, not a new
   parameter), one bounded query per service in the incident's scope: the declared symptom services and the services of the admitted candidates' workloads, at most `N`
   services and `S` spans per query. The spans become `trace_observations()` of the live source; nothing downstream
   changes.
2. **Completeness is evidence, not noise.** A truncated or failed read is recorded as such (the existing completeness
   values) and becomes a coverage fact of the `runtime_traces` channel, as C9 does for the change stream; a rule that
   needs traces treats an incomplete read as unknown, never as absence.
3. **Recorded for replay.** The spans read are persisted with the diagnosis's evidence (the same durable provider
   boundary the investigation reads use), so a replay of the diagnosis sees exactly what the live run saw.
4. **Bounds** (initial, to be confirmed by the measurement of §4, not tuned to labels): `N` = 8 services, `S` = 500
   spans per service, the query timeout of the investigation reads.

## 4. Measurement before adoption

1. **Shadow on the testbed.** Re-run the `DEV` slices (variant A, all six families) with the base read on, the engine
   frozen otherwise: report per run the spans read, completeness, read time, and the diff of the diagnosis against the
   frozen result (claims, support, the shown leader, `root_cause`, digest). Expected, and checked: no new false strong
   authority, no false `RESOLVED`.
2. **C1 in shadow.** On those runs, evaluate the specified service-level effect relation offline and report where it
   holds against the world's chain (propagation links of the ground truth), without adopting it.
3. Only then: adoption of the base read, and a separate decision on C1. Any change of the engine's output is measured
   on a new `HOLDOUT` afterwards (design §12.2.6 of `testbed-scenarios-design.md`).

## 5. Decisions requested

- the base read of §3 (scope, bounds, completeness as coverage, persistence for replay);
- the shadow measurement of §4 before any adoption.

## 6. Implementation (2026-10-03)

`capture_traces` (`packages/rca/live.py`) reads one bounded `query_tempo` per `(namespace, Deployment)` over the newest
hour of the incident window: alert services first, then the listed Deployments, at most 8 services and 500 spans per
service; spans shared by several reads are kept once. Each read's completeness (`BEST_EFFORT`, `TRUNCATED`, `FAILED`
with the error) goes to `trace_captures`; the spans go to `trace_observations` (migration 0031) and enter the run's
manifest as `TRACE` members, so the live run and its replay both read them through `LiveSource.trace_items`. The
boundary event counts them (`traces`). Off unless `SRE_TRACE_CAPTURE=true`.

A first read of the lab's Tempo (2026-10-03) found the spans the engine needs: `CLIENT`, `SERVER`, `CONSUMER` kinds,
`http.response.status_code`, `server.address` on the client side, and `order-worker`'s consumer span inside the same
trace as `order-service`'s request, so the asynchronous edge of C3 is present in the data. Span status is unset
(`UNKNOWN`); success has to be read from the HTTP status code.
