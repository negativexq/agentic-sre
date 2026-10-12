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

## 12. The two windows still miss the execution (owner-approved 2026-10-10)

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

### 12.6 Implementation (2026-10-10)

- `trace_slices` (`packages/rca/live.py`) cuts `[onset − 10 min, min(capture time, onset + 5 min)]` into 60 s slices on
  a grid anchored at the onset.
- The capture (`apps/control_plane/diagnosis.py`) runs one `capture_traces` per (slice, service), at most two at
  once, and skips the slices recorded for the incident in `trace_captures` with the same bounds and no failure
  (`TraceCaptureRepository.read_windows`).
- Spans of skipped slices stay in the incident's later manifests, because a manifest takes every span of the incident
  captured by its end (§6).
- An integration test drives four captures on a moving clock: 12 slices, then only the two new ones, then none, then
  the remaining two up to `onset + 5 min`.

**Blind `phase0` on the lab** (`dependency`, seed 9134, commit `b516fe3`, VALID):
- four incidents, three diagnoses each; only the first diagnosis of each captured, with 33 slice reads (11 slices,
  three services), and no read failed;
- a capture took 1.8 to 3.6 s in all, and a read 0.09 to 0.20 s at the median;
- Tempo did not restart (restart count 20 before and after).

For the incident whose symptom service calls the delayed pod (`OrderDependencyLatencyHigh`), the spans now hold
**11 paired calls inside the execution** and 57 in its §16 baseline. The diagnosis was not read (blind).

### 12.7 A slice within one second is not read (roadmap F15; owner-approved 2026-10-10)

**Measured problem.** On the eighteenth `HOLDOUT` (`testbed-scenarios-design.md` §34.1), all 3 failed trace reads
were one slice of `config-c` #0, read for three services: `17:05:59.192` to `17:05:59.976`. A capture's last slice
ends at the capture time, so it can be shorter than a second. The Tempo reader sends whole seconds
(`_epoch_seconds` truncates), so this slice went out as `start` = `end`, and Tempo answered 400 (`end` = `start` + 1 is
answered, checked by hand). The read was recorded `FAILED` (`ConnectorReadError`), so the run counted as one with an
incomplete read. No other slice can be that short: every slice but the last is 60 s, and the last one ending at
`onset + 5 min` is whole.

**Proposal.**
- `trace_slices` leaves out a slice whose bounds fall within the same whole second, the only bounds the reader can
  express. Nothing is read or recorded for it.
- A later capture of the incident reads that part again as part of its own, longer last slice, since the bounds then
  differ and no read is recorded for the shorter one. Without a later capture, less than a second at the end of the
  read is left out; it is not a failed read, because no read was made.
- Unchanged: the grid, the width, the range, the skip of slices already read, the reader. Widening the slice to a
  whole second instead would make it overlap the slice before it.

**Consequences and measurement.**
- A capture change; the engine version stays, and a stored diagnosis replays from its own manifest as before.
- Unit tests: a last slice within one whole second is left out; one that crosses a whole second is kept; the
  integration test of §12.6 still reads the same slices.
- No `HOLDOUT` of its own: it can only remove a read that cannot succeed. The next full `HOLDOUT` reports failed reads
  as before; any `start` = `end` read there would be a defect of this change.

**Implementation (2026-10-10).** `trace_slices` leaves out a slice whose start and end have the same whole second
(`int(timestamp())`, as the reader's `_epoch_seconds`). Unit tests: the eighteenth `HOLDOUT`'s slice (0.784 s after
the onset) is left out and the slice before it kept; a slice ending 0.9 s after the onset, across a whole second, is
kept; a later capture reads the part left out as its own last slice. The two tests of a left-out slice fail without
the change. The integration test of §12.6 is unchanged and passes.

## 13. A trace is fetched once per capture (owner-approved 2026-10-11)

### 13.1 Measured problem

In the twentieth `HOLDOUT` (testbed-scenarios-design §36) Tempo was killed for memory at 14:37:13, during
`direct-b` #2. That run's last 62 reads failed. By 16:00 its memory had climbed back to 1.8 GiB of its 2 GiB limit.

**Most of the load is the same traces fetched again.** Tempo's own log, from its restart at 14:37 to about 16:10:
- **6,640 trace-by-ID requests** reached it, for **2,150 distinct traces**. 4,490 requests (68%) fetched a trace
  already fetched; one trace was fetched 26 times.
- **3,397 of the repeats (51% of all requests)** came within two minutes of the trace's first fetch, so within the
  same capture. 1,093 came later, from another capture.
- **Each request inspected 17 to 57 MB.** The searches report `total_blocks=0`: the recent minutes are still in the
  ingester's open block, and a lookup by ID scans it whole. Bursts reached 264 requests and 3.7 GB inspected in a
  minute.

**Why the same capture repeats a trace:**
- A search is made per service and per slice (§12), and keeps up to 8 traces.
- An order's trace holds spans of `order-service`, `payment-service` and `order-worker`. Each service's search finds
  it, and each fetches it.

### 13.2 Proposal

**Within one capture, a trace is fetched once.**
- The capture keeps the trace IDs it has fetched, with their spans. A search that returns an ID already fetched in the
  same capture reuses those spans and does not ask Tempo again.
- The capture's reads are made at the same moment, so a reused trace is what a second fetch would have returned.
- Each read still records its own completeness, span count and slice (`trace_captures`), and every span is stored as
  today.

**Unchanged:**
- the range and its slices (§12);
- the services read;
- at most two reads reaching Tempo at once (§11);
- the 8-trace search limit and the span cap;
- persistence, replay, and completeness as coverage (§3.2).

**Not decided here:** reusing a trace fetched by an earlier capture (the other 16% of the repeats). An earlier fetch
may have been made before all of the trace's spans arrived, for example a consumer's span. Reusing it could drop
spans, so it needs its own rule.

### 13.3 Consequences and measurement

- **No span changes.** A reused trace's spans are those the same capture fetched. A unit test shows that a capture
  with reuse stores the same spans as one without.
- **The engine is unchanged.** The capture belongs to the control plane, so this needs no engine version.
- **Measured in the next full `HOLDOUT`,** together with the lab's Tempo settings (testbed-lab-design §15), against
  §36:
  - trace-by-ID requests per incident, read from Tempo's log;
  - Tempo restarts and its memory peak;
  - failed reads per run.

### 13.4 Implementation

- `ProviderAdapter.fetching_traces_once()` gives each `_capture_traces` call its own `TraceFetchCache`. Nothing is
  kept across captures.
- A trace-by-ID request is reused only when it is identical: same trace ID and same range. The services' searches of
  one slice share that range, so they share their fetches. Another slice is another request.
- A 404 is reused as missing. Any other failure is not kept: the next read asks Tempo itself.
- Two concurrent reads of the same trace wait for one fetch.
- Unit tests in `test_tempo_backend.py` show that reads with reuse return the same batches as reads without, with
  fewer fetches.
- **Superseded by §14.2.** The sequential reuse and the kept 404 are withdrawn; only concurrent identical fetches are
  joined.

## 14. Trace reads through the Connector: fetch once (P0) and one shared read budget (P1) (2026-10-11, owner-approved with the revisions of §14.2 and §14.3)

### 14.1 Measured problem

**§13 never ran on the lab's path** (testbed-scenarios-design §37.1):
- The lab's control plane reads Tempo through the Connector (`SRE_CONNECTOR_MODE=remote`).
- `ConnectorTempoReader` has no `fetching_once`, so `fetching_traces_once()` returns the adapter unchanged.
- The Connector makes the HTTP requests, and its image does not contain #112.
- `tests/unit/rca/test_connector_trace_fetch_once.py` shows it on the real path (`ProviderAdapter` →
  `ConnectorTempoReader` → `ConnectorClient` → encoded bytes → `Connector` → `TempoTraceReader`): one capture of
  three services sends **6 trace-by-ID requests where 4 are distinct**. The test is marked `xfail(strict=True)` until
  P0 lands.

**The read budget is per incident, not shared:**
- `_capture_traces` reads with `max_workers=2`, but each incident's diagnosis runs on its own thread
  (`diagnose_in_background`), and no lock spans them.
- In the twenty-first `HOLDOUT` two incidents captured in the same second, so up to four reads reached Tempo at once.
- The bulk log capture already has the shared slot it needed for the same reason (`_log_capture_slot`).

**From Tempo's own log, the first 1.5 runs of the twenty-first `HOLDOUT` (16:33–16:45):**
- 1,525 trace-by-ID requests, 575 distinct requests (same ID, same range), 241 distinct traces.
- One run opened five incidents. Each captured the same slices of the same three services.
- 60 s capture reads: 1,127 requests, 6.6 GB inspected (5.9 MB per request).
- Wide investigation reads (about 31 min): 190 requests, 0.4 GB.

**Tempo 2.8.2's metrics, read from the running pod's `/metrics`:**
- **Requests:**
  - `tempo_query_frontend_queries_total{op="traces"|"search", result}`;
  - `tempo_request_duration_seconds_count{route="api_v2_traces_traceid", status_code}`.
- **Failures:**
  - the same two series, with `result` other than `completed`, or `status_code` other than `200`;
  - `tempo_query_frontend_retries`.
- **Concurrency:**
  - `tempo_inflight_requests{route="api_v2_traces_traceid"|"api_search"}`;
  - `tempo_query_frontend_queue_length`;
  - `tempo_query_frontend_queue_duration_seconds`.
- **Memory:** `process_resident_memory_bytes` and `go_memstats_heap_inuse_bytes`. The kind node's cgroup files, as
  sampled today, are the limit's view.
- **Bytes inspected:** Tempo 2.8.2 exposes no metric for it. The name proposed in review
  (`tempo_query_frontend_bytes_inspected_total`) does not exist in this version. It stays read from the log: the
  `inspected_bytes` field of `msg="trace id response"`.
- **Sharding:** each trace-by-ID request is split into 50 querier jobs (`query_frontend.trace_by_id.query_shards`,
  default 50). 3,930 frontend requests ran as 196,500 querier requests. This is recorded here and not changed by
  this section.

### 14.2 P0: concurrent identical fetches are made once, through the Connector (revised by the owner, 2026-10-11)

**Base rule: single-flight only.**
- Within one capture, identical trace-by-ID requests to Tempo that are **in flight at the same time** are made once.
  Each waiter receives that one response.
- Once the fetch is done, **nothing is kept**. The next identical request goes to Tempo.
- **What a waiter receives:** Tempo's state when the first request was served. That is at most one fetch's duration
  (milliseconds to a second) before the waiter would have asked. It is what the waiter would have received had it
  asked that much earlier. A span reaching Tempo in that interval is seen by the next read, as today.

**The scope travels with the read:**
- `_capture_traces` makes a random 128-bit `capture_scope` (32 hex characters).
- `fetching_traces_once()` binds it:
  - to a direct `TempoTraceReader`;
  - to a `ConnectorTempoReader`, which then sends it with every `query_traces` of that capture.
- The scope is opaque. It names no incident, service or object.
- Requests of different scopes are never joined. Reuse across incidents is P2.

**The wire is extended, and negotiated (amends connector-boundary-contract §3 and §4):**
- `query_traces` accepts an optional `capture_scope` (`^[0-9a-f]{32}$`).
- A Connector that accepts it lists the capability `trace_capture_scope`.
- The client sends the field only to a Connector that lists it. An older Connector rejects unknown fields
  (`extra="forbid"`), so it is never sent one, and its reads stay as today.
- `WIRE_VERSION` stays `connector.v2`.

**Key:** backend URL, tenant, scope, trace ID, exact `start` and `end`. Another backend, tenant, scope or range is
another key.

**Bounded:**
- Only in-flight fetches are held: at most the number of concurrent reads.
- A scope's bookkeeping ends when its last waiter returns.
- A response is bounded by the existing 4 MiB limit.

**A failure is never shared beyond the fetch that failed:**
- Any failure (HTTP error, timeout, oversized or invalid response) is raised to every waiter of that fetch, as if each
  had made it, and is not kept.
- A 404 is not kept either. A trace missing at one moment can appear when its spans arrive. This replaces §13's
  reuse of a 404 for the direct reader too: §13's sequential reuse inside a capture is withdrawn for the direct reader
  as well, so both paths follow this rule.

**Sequential reuse inside a capture is not adopted:**
- **The idea:** reuse a finished fetch for a later identical request in the same capture, for example only for a
  range that ended 60 s before the fetch.
- **Why it is not a correctness rule:** 60 s does not prove that every span of the trace was emitted and ingested.
  The exporter's 5 s schedule and 30 s timeout say nothing of when an asynchronous Kafka consumer runs; a delayed
  consumption can add a span minutes later.
- **What it may become:** a performance assumption tested on its own, behind a flag that is off. It needs a design
  for freshness and span equivalence before it is used.

**Deterministic tests:**
- **Late spans, with a fake Tempo whose traces change over time:**
  - a delayed Kafka consumer span added after a first fetch is present in the next identical read of the same
    capture;
  - late ingestion: a trace first 404, then present, is found by the next read;
  - a partial trace (producer only) shared by concurrent waiters, then complete, is complete in the next read.
- **Single-flight:**
  - two and eight concurrent identical requests make one HTTP fetch;
  - a failure reaches every waiter and is not kept;
  - different scopes, tenants or ranges are never joined.
- **Compatibility:** an old Connector (no capability) receives no `capture_scope`.
- **The Connector-path test:** it fails before P0 and passes after. Its `xfail` is removed in the P0 commit, so it
  does not stay.

**Verification mode (off by default):**
- Every joined waiter also fetches from Tempo itself, compares the span sets (`trace_id`, `span_id` and the parsed
  span) and keeps the fresh fetch, so verifying changes no data.
- Every difference is logged with its trace ID and the spans that differ.
- **Acceptance:** a `DEV` run with verification on, with 0 differences. It is an acceptance check, not proof that no
  late-span case can differ. The deterministic tests above cover those.

**Counted:**
- **Per scope, logged by the Connector when the scope ends:**
  - requests;
  - joined waiters;
  - actual HTTP fetches;
  - failures;
  - verification differences.
- **Cumulative totals:** the Connector's `preflight` reports them.

**Expected gain: modest.** Only reads that overlap in time are joined: the two workers of one capture reading one
slice. The counters measure it. A larger reduction needs P2 or a proven sequential reuse.

### 14.3 P1: one shared capture-read budget in the control plane (revised by the owner, 2026-10-11)

**Rule:**
- The `Diagnoser` holds one **FIFO** admission gate with **2 slots**, `_trace_read_slots`, shared by every incident's
  capture.
- Each single capture read (one search and its at most 8 fetches, for one service and one slice) is admitted for
  that read and releases its slot after, in a `finally`, whatever the read's outcome.
- `max_workers=2` per capture stays.

**Fairness and starvation:**
- Waiters are admitted strictly in arrival order (a ticket queue). Python's `threading.Semaphore` does not promise
  that.
- No waiter is passed by a later one. A read therefore waits at most for the reads queued before it.

**Why per read and not per capture:**
- A lock around the whole capture, as for logs, would make a fifth incident wait for four whole captures.
- Per-read admission keeps capture reads at two at a time, and the incidents' reads interleave.

**Admission timeout is its own class:**
- A read waits at most **120 s** to be admitted.
- If it is not admitted, it is not sent. It is recorded with completeness `FAILED` and error
  `TraceReadAdmissionTimeout`.
- That error is distinct from every backend error (`TempoHttpError`, timeouts, `ProviderReadFailure`), and the two are
  counted separately.
- **Epistemically it stays `FAILED`:** a read not made is coverage, never absence (§3.2), and m21 §29 C stays blocked
  (`TRACE_READ_FAILED`) for that caller.
- **A waiter that times out or is cancelled leaves the queue without taking a slot.** No slot is lost.

**What it bounds, and what it does not:**
- **Bounded:** the control plane's capture reads, to two at once.
- **Not bounded: the engine's investigation reads (`ENGINE` caller class).** Delaying them could change what an
  investigation sees, and engine 2.10.0 is not changed.
- **So this is not a global bound on Tempo's concurrency.** The `CAPTURE` and `ENGINE` loads are measured
  separately: from the provider read records (`caller_class`) and from Tempo's log by range width.
- **More than one control-plane replica:** the gate is per process. Each replica would allow two.
- **The global point is the Connector,** one per cluster. If the measurements call for it, a total bound there is a
  separate design.

**Tests, at 5 and 10 concurrent incident captures,** against a fake reader with fixed latency:
- at most 2 reads in flight at any time;
- admission strictly in arrival order;
- every read completes;
- total time within one read's latency of reads × latency / 2;
- no incident's capture waits for another's to finish whole;
- a short admission wait times out late reads as `TraceReadAdmissionTimeout`, distinct from a backend failure, and
  the capture still completes;
- a timed-out or failing read leaves no slot held.

### 14.4 Unchanged, and not decided here

**Unchanged:**
- engine 2.10.0;
- the slices and services read (§12);
- the 8-trace search limit and the span cap;
- persistence, replay, and completeness as coverage;
- the twenty-first `HOLDOUT`'s frozen code and criteria.

**Not decided here:**
- **P2,** reuse across incidents or captures;
- **sequential reuse inside a capture** (§14.2);
- **a Connector-side total bound** (§14.3).

**Delivery:**
- P0 and P1 are separate commits.
- **The Connector may be rebuilt locally and used in tests.** Deploying it, or the control-plane change, to the
  running lab needs the owner's separate approval.
- **No new `HOLDOUT` is started by this section.**

### 14.5 Planned after P0 and P1: Tempo's trace-by-ID fan-out (`query_shards`), an isolated A/B

**Not changed now.** Tempo splits each trace-by-ID request into 50 querier jobs.

**The plan, to be pre-registered before it runs:**
- **Data:**
  - a copy of the lab Tempo's stored blocks, taken after the twenty-first `HOLDOUT` ends;
  - the recorded trace-by-ID requests of one `HOLDOUT` suite, from the streamed log.
- **Where:** a separate local Tempo 2.8.2, one per setting, outside the lab.
- **Settings:** `query_shards` 50 (default), 16 and 4. Nothing else differs.
- **Replay:** the same requests, in the same order and concurrency.
- **Measured:**
  - latency per request (p50, p95);
  - bytes inspected;
  - CPU;
  - memory peak;
  - **trace correctness:** each response's span set compared with the default's, where any difference fails the
    setting.
- **Lab:** no setting reaches the lab without the owner's approval.

### 14.6 DEV validation of P0 and P1 on the lab (2026-10-11, owner-approved)

**The run is not valid.** It ended `INVALID: missing recovery_at`, the same harness defect as dev20b. Its diagnoses
are not scored and are not evidence for engine 2.10.0. **Its load and read measurements are kept**: they do not
depend on the recovery time.

**Identities:**
- **Code:** main `c1c8f25` (engine 2.10.0, §14.2 P0, §14.3 P1); `make check` 2812 passed, 25 skipped.
- **Control plane:** run by the harness from a worktree at that commit.
- **Connector:** `agentic-sre/connector:dev`, built locally from a clean worktree at that commit, label
  `org.opencontainers.image.revision=c1c8f25`, digest `sha256:e2c96279f0db…`.
  - The image it replaced is kept in the kind node as `agentic-sre/connector:before-p0` (`sha256:8df6e56c…`).
  - **It stays in the lab** (owner, 2026-10-12); no rollback.
- **Verification mode** (`TEMPO_VERIFY_SINGLE_FLIGHT`) was on for this run only and removed after it.

**The run:** suite dev21-kafka-delay, `dependency-b-kafka-delay`, seed 2101, one run, 21:21–21:34 UTC.
- **Smoke:** capabilities listed `trace_capture_scope`; `preflight` reported `trace_fetches`; a scoped
  `query_traces` returned 80 spans.
- **Guard:** never triggered (Tempo memory above 1800 MiB, or any FAILED trace read).

**Measured, against the twenty-first `HOLDOUT`'s `dependency-b` window:**

| | DEV21 | HOLDOUT21 dependency-b |
|---|---|---|
| Tempo memory peak | 1,093 MiB | 1,816 MiB |
| Tempo trace-by-ID concurrency, max | 2 | 4 |
| FAILED trace reads | 0 | 0 in that window |
| ENGINE (`INVESTIGATION`) reads per incident | 24 | 16–20 |

- **CAPTURE:** 856 trace-by-ID requests, 328 distinct.
- **P0, the one scope summary logged:** 200 requests, 4 joined (2%), 0 failures, 0 verification differences.
  - In verification mode a joined request is also fetched for comparison, so `http_fetches` (200) counts those.
  - No "differs" warning in the whole Connector log.
- **P1:** no admission timeout. The per-capture wait summaries were not recorded (below).

**What this does and does not show:**
- One run, one scenario. The lower peak and concurrency are consistent with P1's shared budget; they are not a
  measured effect size.
- **P0 joins little here:** most repeats in a capture are sequential, not concurrent (§14.2 leaves sequential reuse
  undecided).
- The ENGINE count is higher than the window's; one run does not say why.

**Two observability gaps, closed before the next `HOLDOUT` (owner, 2026-10-12), with no new protocol:**
- **P0:** a scope summary is logged only when a later request sweeps idle scopes, so most scopes never report.
  - The harness reads the Connector's `preflight` `trace_fetches` totals at the start and the end of each run and
    records both and their difference in the run journal.
- **P1:** the per-capture wait summary is an INFO line, and the lab control plane logged only WARNING.
  - The lab control plane now runs with the existing `SRE_LOG_LEVEL=INFO`, and the harness copies the run's
    summary lines into the run directory.

## 15. A slice's services share their trace fetches (P2, 2026-10-12, owner-approved)

### 15.1 Measured problem

**Source:** the twenty-second `HOLDOUT`'s 24 run databases: every trace read, with its fetched trace IDs and spans.

**Logical trace-by-ID fetches:** 29,494, of which 26,854 were CAPTURE and 2,640 were ENGINE.

| Fetch, against everything fetched before it in the run | Share | Returned a span not seen before |
|---|---|---|
| First fetch of the trace | 18.3% | — |
| Same trace and range, same capture | **46.7%** | 0 of 13,770 |
| Same trace and range, another incident | 17.0% | 0 of 5,035 |
| Same trace, another range, another incident | 11.8% | 4 of 3,861 |
| Same trace, another range, same incident (mostly ENGINE) | 4.5% | 0 of 1,323 |

- **Cause: the capture plan.** Each 60 s slice searches each service on its own. A request's trace crosses
  `order-service`, `payment-service` and `order-worker`, so the three searches select the same traces, and each read
  fetched them again.
- **Why P0 missed most of them:** the repeats are sequential, not concurrent. The median gap is 0 s, 99% come within
  10 s, and the longest is 21.6 s.
- **The potential, from a simulation:** about 46% fewer trace-by-ID requests and bytes inspected. This is a
  potential, not a target.

### 15.2 The change

**Each slice is read as one unit:**
- each service's own search runs, with its own 8-trace limit;
- every trace the searches selected is fetched once, for the slice's range;
- each service's read is built from the traces its own search selected;
- each read is recorded on its own, as before: target, observation, diagnostics and evidence IDs. Its timing is the
  whole slice call's.

**Unchanged:**
- **Nothing is kept after the slice.** The next slice, and the next capture, fetch again, so a late span (a Kafka
  consumer's) is read by the next read, as §14.2 requires.
- **A failure** fails only the reads that needed it:
  - a failed search fails its own service's read;
  - a failed fetch fails the reads whose search selected that trace;
  - a trace that only failed reads selected is not fetched.
- **ENGINE reads** still query one target at a time.

**P0 and P1:**
- **P0:** the slice's fetches still go through the capture's single flight.
- **P1:** a slice now takes one slot, for its searches and fetches together. Captures still have at most two Tempo
  requests in flight.
  - A slice not admitted within the wait fails each of its reads with `TraceReadAdmissionTimeout`.
  - The capture's summary line counts reads and slices.

**Connector:** `query_traces_many`, negotiated by `trace_slice_fetch` (connector-boundary-contract §19). An older
Connector is read one service at a time.

**Not in this change:**
- fetches repeated across incidents;
- slice boundaries;
- any reuse of a finished fetch.

**Measured after it:**
- Tempo HTTP requests;
- bytes inspected;
- capture duration and admission waits;
- evidence equivalence and diagnosis accuracy, against the same scenarios read before it.

### 15.3 Measured result (2026-10-12, owner-accepted)

**Runs:**
- **A/B on the same data:** 240 slices of the lab Tempo's stored traces, read once one service at a time and once as
  slices, at `a00f6ea`.
- **DEV22:** `dependency-b` seeds 2201 and 2202, and `config-c` seed 2203, at `a00f6ea`; 3 of 3 valid.
- **DEV22m:** the same suites again on a freshly restarted Tempo, with the Tempo container's `memory.stat` every
  15 s; 3 of 3 valid.
- **Before:** the twenty-second `HOLDOUT`'s runs of the same two variants.

**Tempo requests and bytes inspected:**

| Measure | Before | After |
|---|---|---|
| A/B: HTTP requests | 6,246 | 2,814 (−55%) |
| A/B: trace-by-ID fetches | 5,526 | 2,094 (−62%) |
| A/B: searches | 720 | 720 |
| A/B: bytes inspected | 119.1 GB | 44.9 GB (−62%) |
| Live: Tempo fetches per logical CAPTURE fetch | 0.82–0.98 | DEV22 0.32–0.51, DEV22m 0.54–0.75 |
| Live: bytes inspected per CAPTURE read | 19–95 MB | DEV22 14–18 MB, DEV22m 13–54 MB |

- 238 of the 240 A/B slices had identical evidence. The other 2 differ because of a Tempo search race: with two
  block jobs and an 8-trace limit, Tempo answers after the first job completes. The difference repeated the same
  way on a second run, so it does not come from the change.
- Live bytes per read follow how many blocks Tempo holds, so they vary between runs more than requests do.
- The Connector's totals counted 0 single-flight joins and 0 failures. The whole saving is §15.2's.

**Capture timing:**

| Measure | Before | After |
|---|---|---|
| Admission wait per run, total | 0–504.6 s | 0–55 s |
| Admission wait, longest single | up to 7.4 s | up to 4.8 s |
| Capture duration, median | 5.4–66 s | 1.8–7.4 s |
| Alert to first diagnosis | 47.6–94.5 s | 45.9–56.8 s |

The wait is now counted per slice, not per read.

**Tempo memory: the peak did not drop.**

| Run | Container peak | anon | file | RSS | heap |
|---|---|---|---|---|---|
| DEV22m `dependency-b` | 1,506 MiB | 1,244 | 30 | 1,276 | 1,192 |
| DEV22m `config-c` | 1,861 MiB | 1,496 | 56 | 1,556 | 1,296 |
| Before, the same variants | 1,645 / 1,663 MiB | — | — | 1,327 / 1,368 | 925 / 1,099 |

- **What the samples show:**
  - Idle, Tempo's anonymous memory is 200–370 MiB.
  - Each capture burst raises it to 1.15–1.5 GiB, and it falls back within one to two minutes.
  - The burst peaks grew as Tempo stored more: 1,299, 1,506, 1,606, then 1,860 MiB.
- **Reading, not proven:** the peak follows how much data the concurrent queries hold, not how many requests there
  are. §15.2 lowers the count and leaves the concurrency unchanged.
- **DEV22's container figures are not used.** They held about 330 MiB of page cache left by the A/B run before them.
- **Not comparable at the start:** before the restart, the previous Tempo's `memory.peak` had reached its 2 GiB
  limit. Its last start was the cluster's own, about an hour before.
- **Open:** the Tempo memory peak stays an open risk, outside this change.

**Diagnosis:**
- Every run had cause 1.00, instance 1.00 and every witness correct.
- No false strong authority, false resolution or decoy was named.
- No trace read failed.
- §19, §21 and §29 had no violations.
- Resolution by alert matched the runs before:
  - the order-side alerts are `AMBIGUOUS` everywhere;
  - the payment-side alerts are `RESOLVED` whenever they fire. They fired in one run before and one in DEV22, and
    in none of DEV22m.

**Verdict:**
- P2 is accepted: requests, admission waits and capture time fell, and evidence and accuracy are unchanged.
- The performance work is closed for now.
- The `query_shards` A/B and the fetches repeated across incidents (§15.1, 17.0% and 11.8%) are not pursued.
- The sample is small: 3 runs a variant set.
