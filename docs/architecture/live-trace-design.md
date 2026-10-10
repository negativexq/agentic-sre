# Live traces in the base diagnosis (design, owner-approved 2026-10-03)

Status: **APPROVED** by the owner (2026-10-03) and implemented behind `SRE_TRACE_CAPTURE` (on by default since 2026-10-04,
§11). Roadmap A6, prerequisite of C1, C3 and the effect side of C5.

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

## 7. Shadow measurement (2026-10-03)

The six `DEV` families (variant A, the frozen seeds) re-run with `SRE_TRACE_CAPTURE=true`, the engine frozen at
`a92ee1f`; slices 2 and 3 were rerun after two harness fixes (`4dfd4e1`: a dead port-forward is stood up again; an
alert the control plane admits after the cause is gone still stamps the timeline). **18 of 18 runs valid.**

Every stored diagnosis was replayed twice from its own manifest, with its captured spans and with them removed, so the
difference is due to the spans alone (the Connector's alert-admission change of §16 is the same on both sides):

| Diagnoses | With spans | Changed by the spans (root cause, confidence, resolution, shown leader, support, strong, digest) |
|---|---|---|
| 271 | 271 | **0** |

The spans reach the engine: in one run 216 spans, all bound to a service, gave a runtime graph of 3 services and 2
edges (`order-service` → `payment-service` by client and server spans; `order-service` → `order-worker` by a fallback
parent link) and 27 call pairs. Nothing changed because no rule consumes trace evidence yet (C1 is not implemented).
The base read is therefore safe to turn on: it adds evidence and disturbs no decision.

Two findings for C1, which the specified relation does not yet handle:

1. **Latency faults leave no non-success call.** The 27 call pairs of that run were all successful (HTTP 201) while
   `payment-service` was delayed; the specified relation reads non-success edges only, so it can never hold for the
   delay and loss families. A latency relation (span duration against the run's own baseline) has to be specified.
2. **The sample can miss the fault.** One read per service returns at most 32 traces from the newest hour of the
   window, not necessarily from the fault's interval. A relation that needs the fault interval has to read around the
   onset.

Separate finding, not caused by the spans: replaying with spans reproduced the live diagnosis's digest for 200 of 271
diagnoses; the other 71 differ with and without spans alike, so the gap lies between live and replay, and is to be
examined on its own.

## 8. Read time, and a focused read (2026-10-03)

The read time §4.1 asked for, from the provider read records of the shadow runs: one Tempo read took 0.8 s median,
4.7 s at p90 and 19.5 s at worst (1,134 reads, 29 failed); **the trace read of one diagnosis took 8.6 s median, 27 s
at p90, 45 s at worst** (162 diagnoses). Too long to turn on for no decision yet. Two causes, both changed:

- every listed Deployment was read, including Kafka, Postgres, Redis and the isolated workload, which emit no traces
  and always returned none: only the alert services and the Deployments whose pod template configures OpenTelemetry
  (an `OTEL_*` environment variable) are read now;
- each read searched the newest hour of the two-hour lookback, which is slow and can miss the fault: the read now
  covers ten minutes before the first alert through the capture, at most one hour.

Measured again before the default changes.

## 9. The focused read held only the quiet minutes (2026-10-03)

Remeasured on slices 1 and 3 (6 runs, 51 diagnoses, all valid): three reads per diagnosis, **2.8 s median, 11.2 s at
p90, 20.5 s at worst**, no failed read. But all **5,598 stored spans began before the fault**, none during it: Tempo
returns a window's oldest traces first, and with the window opening ten minutes before the first alert, the 32
traces of each read were always the quiet traffic before the fault. The earlier newest-hour read had the same flaw, so
the shadow result of §7 ("0 of 271 changed") is weaker than it looked: the engine may never have been shown the fault.

Changed: two narrow reads per instrumented service, the **fault** from two minutes before the first alert to five
minutes after it (an alert fires about a minute after its fault begins), and a **baseline** from ten to five minutes
before it, which a latency relation (§7, finding 1) has to compare the fault's calls with. Measured again: the share of
spans inside the fault's interval and the read time.

## 10. Two narrow windows: result and decision (2026-10-03)

Slices 1 and 3 again (6 runs, all valid) with the fault and baseline reads of §9:

- **the fault is read now**: 1,096 stored spans began inside the fault's interval (124 to 208 per run), where the
  earlier reads held none;
- **still no decision changes**: 77 diagnoses replayed with and without their spans, 0 changed, now with the fault in
  the spans, which confirms §7 on evidence that shows the fault;
- **read time**: 6 reads per diagnosis, 7.8 s median, 24.8 s at p90, 32.2 s at worst (45 diagnoses); Tempo's read
  time varies widely between runs, and the reads are sequential.

**Decision (owner, 2026-10-03): the default stays off.** The read adds about eight seconds to a diagnosis and changes
nothing until a rule consumes traces. It is turned on in the measurement runs of C1, and the reads are made concurrent
with C1, when the evidence starts to matter.

## 11. On by default, concurrent reads (owner, 2026-10-04)

C1 (m21 §12.7) now consumes the spans, so the read is on by default (`SRE_TRACE_CAPTURE=false` turns it off). The
two windows are read at once, each one service after another, so at most two reads reach Tempo together. Reads of
every service at once (up to 16 searches) overloaded the lab's Tempo in the second `HOLDOUT` (testbed-scenarios-design
§13.1: 15 of 18 runs had failed reads, Tempo restarted), so the owner set the limit at two (2026-10-04). Tests that assert a run's exact provider-read sequence
for another purpose turn the read off explicitly.

## 12. The two windows still miss the execution (proposal, 2026-10-10, awaiting the owner)

### 12.1 Measured problem

The capture reads two windows per instrumented service (§9, `apps/control_plane/diagnosis.py`): the **fault** from two
minutes before the first alert to five minutes after it (at most the capture time), and a **baseline** from ten to five
minutes before it. Each search keeps at most 8 traces (`_MAX_SEARCH_TRACE_IDS`), and on the live path Tempo returns
the earliest ones first (§9). Three consequences, measured on the seventeenth `HOLDOUT`
(`testbed-scenarios-design.md` §32.1):

- **The fault window yields its first seconds, which lie before the execution.** An alert fires about 20 to 30 s after
  the execution in the testbed, so the window opens about a minute and a half before it. In `dependency-b` #2 the
  `OrderErrorRateHigh` capture ended its spans at 23:36:19; the experiment was applied at 23:36:36.
- **The engine's baseline is mostly not read.** The effect relations compare with the five minutes before the first
  execution (m21 §16), about `[onset − 6 min, onset − 1 min]`. The capture reads `[onset − 10, onset − 5]` and then
  nothing until `onset − 2`.
- **Across all reads:** in 298 of 1,061 successful trace reads of the seventeenth `HOLDOUT` (28%), the latest returned
  trace began in the first tenth of the requested window.

Of the 12 `dependency-b` and `competing-b` incidents whose symptom service calls the faulted `payment-service`, 6 held no
call inside the execution at all; the other 6 held only calls that never reached the target (§12.4).

### 12.2 Proposal

**One contiguous range read in one-minute slices.**

- The range is `[onset − 10 min, min(capture time, onset + 5 min)]`: the same outer bounds as today's two windows, with
  the gap between them closed. It covers the engine's baseline wherever the execution falls in the five minutes the
  contract admits before the onset.
- The range is cut into slices of 60 s on a grid anchored at the onset. Each slice is one search per service, with the
  same 8-trace limit, so every minute contributes its own sample instead of the range contributing its first seconds.
- **No slice is read twice for an incident.** A later capture of the same open incident skips a slice whose read for
  that service is already recorded with the same bounds and did not fail (`trace_captures`). Only the slices after the
  previous capture, and the last partial one, are read again.
- **Unchanged:** the services read (§8), at most two reads reaching Tempo at once (§11), the span cap per service, the
  persistence and replay (§6), and completeness as coverage (§3.2): a failed or truncated slice is recorded as such.

### 12.3 Shadow on the seventeenth `HOLDOUT` (run before this text, stated as such)

The lab's Tempo still held the seventeenth `HOLDOUT`'s traces. For every incident, the recorded capture windows and the
proposed slices were searched again directly, with the same query and limit, and a trace of the symptom service that
began inside an execution's `[Applied, Recovered]`, or inside its §16 baseline, was counted. These were search results
only: no trace was fetched, and a trace's start stands for its call. 73 (incident, chaos execution, symptom service)
rows:

| | Current plan | Slices of 60 s |
|---|---|---|
| Rows with at least 3 traces inside the execution | 8 | **48** |
| Rows with at least 3 traces in the §16 baseline | 68 | 69 |
| Traces returned in all | 1,202 | 3,888 |

**Caveats:**
- Tempo now serves these runs from compacted blocks. Its order may differ from the live path, where the data sat in the
  ingester, so the live effect is to be measured live.
- Rollout changes (`config-*`, `negative-b`) are not in this count: it uses the chaos executions only.
- More calls inside the execution do not by themselves form a witness. A packet loss leaves calls that never reach the
  target (§12.4), and the relation still needs paired calls.

### 12.4 Not in scope

The calls a packet loss or partition leaves are client spans with no server span. The relation pairs a client span
with its server span, so these calls never count. That is roadmap C2, proposed separately after this one, because
it cannot be measured while the read misses the execution.

### 12.5 Cost and measurement

- **Searches:** about 15 per service for the first capture of an incident instead of 2, and only the new slices
  afterwards. The lab reads three services. With two reads at a time and the 0.8 s median of §8, a first capture takes
  about 20 s more, plus the trace fetches (up to 8 per slice).
- **Tempo's memory:** the lab's Tempo was killed for memory once in the seventeenth `HOLDOUT` (01:49 UTC, `negative-c`
  #0's 13 failed reads). Search load grows with this change, so the measurement reports Tempo restarts and failed reads
  beside the read time.
- **Measurement:**
  1. A blind `phase0`: read time per capture, slices read and skipped, failed reads.
  2. Then a full `HOLDOUT`, pre-registered, which covers this change and F13 (`testbed-scenarios-design.md` §33)
     together, as the owner decided (2026-10-10). It reports:
     - the share of executions with calls read inside them, against the seventeenth;
     - execution-witness recall, for information only.
  This is a capture change, not an engine change: the engine version stays, and a stored diagnosis replays from its
  own manifest as before.
