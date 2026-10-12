# Payment phase telemetry: bounded feasibility measurement

2026-10-12, pre-registered before running the experiment. This is a development
measurement, not §32 approval, a causal rule, or a HOLDOUT.

## Scope and fixed criteria

Run the actual payment HTTP application on the host with a new, experiment-owned
PostgreSQL database. Keep the lab deployments, resource limits, capture search
limit (8 traces), engine 2.11.0, C, and HOLDOUT23 unchanged. Optional new spans
observe connection acquisition, transaction waiting, the previously unobserved
query, and authorization. They contain no injected-fault or causal labels, no SQL
text and no request contents. The option defaults off.

Compare existing telemetry with the option enabled, alternating A/B/B/A. Per arm:
10 warm-up requests, 100 normal requests, and 8 each of connection-pool contention,
application transaction wait, delayed SQL execution, and a real blocked PostgreSQL
table operation; also 5 early request failures. Induced waits are 120 ms; normal
operations use the same database and application. Pool and table locks are released
after each request and the temporary database is removed when the experiment ends.

- **Evidence:** in at least 80% of enabled requests in each wait class, the known
  wait is located in its correct observed phase (duration at least 80 ms). A DB
  block must remain inside the DB call, not be classified as application-local.
  Early failures must show an error in authorization and no false DB execution.
- **Volume:** report spans and encoded OTLP bytes per request, including the
  number of new spans on normal traffic (expected two).
- **Application cost:** report SERVER-span and HTTP-client durations separately.
  Enabled normal-request median SERVER duration must not exceed the baseline by
  more than max(10%, 1 ms). This is an exploratory cost bound, not a production SLO.
- **Capture:** export to the running Tempo under unique experiment service names,
  then search/fetch with limit 8. Report retention of the new phases in fetched
  traces. No query/capture limit increase.
- **Memory:** sample Tempo process RSS and Go heap at 250 ms intervals throughout
  the experiment. Report sampled peaks and before/after values. A rise above
  64 MiB calls for investigation; small-load results cannot establish safety at
  sustained testbed load or causally isolate unrelated Tempo background work.

Proceed only if the new observations separate the wait classes without an
excessive measured cost. If evidence remains indistinguishable, do not pursue a
new witness rule based on this experiment. Exact alarm/metric coverage, replica
controls, and causal sufficiency are not established by locating a wait.

## Results

Completed on the host, using real PostgreSQL operations and the running lab Tempo.
Four arms, A/B/B/A, each 147 requests (588 total). The 400 normal measured requests
exclude 40 warm-ups. No lab Deployment, resource limit, RCA rule or capture limit
was changed. All successful scenarios returned 201; early failures returned 500.
The experiment-owned databases were removed. This tests operation placement and
small-load overhead; it is not a rollout, replica or §32 causal validation.

| Wait/failure class | Existing telemetry: located | Added phases: located |
|---|---:|---:|
| Connection-pool contention | 0/16 | 16/16 |
| Application transaction wait | 0/16 | 16/16 |
| Previously unobserved SQL wait | 0/16 | 16/16 |
| Real DB table-lock wait | 16/16 | 16/16 |
| Early authorization error | 0/10 | 10/10 |

The location check uses the induced experiment outside the telemetry as its
reference and requires at least 80 ms in the expected phase. It does not infer
that a normal DB call excludes every possible dependency fault. Pool contention
is local acquisition waiting, not proof that the remote database was slow.
The real DB lock remains in the existing DB CLIENT span. New phase spans carry
no fault labels, SQL text or payload contents. The experiment uses an injected
session factory on the host, so Pod/UID binding and declared peer attribution
are not tested here. In-span evidence is not promoted to strong causal authority.

| Normal requests | Option off | Option on |
|---|---:|---:|
| SERVER median | 2.591 ms | 3.385 ms |
| HTTP client median | 2.916 ms | 4.100 ms |
| Spans per request | 2 | 4 |
| Encoded OTLP bytes per request | 640 | 824 |

The SERVER increase is 0.794 ms (31% relative), within the pre-registered 1 ms
absolute allowance. HTTP-client increase is 1.184 ms; TestClient, scheduling and
database round trips contribute to it. Individual enabled-arm SERVER medians
were 2.360 and 3.970 ms, indicating substantial run variability. This is not a
production throughput estimate. OTLP bytes are per-request protobuf sizes, not
measured compressed network traffic; the increase is about 29%. Wait scenarios
can add another phase span. Both arms use the opt-in code with the option off/on,
not a binary benchmark of the unmodified main revision.

**Capture:** with the unchanged limit 8, V2 reads in the two enabled arms returned
8 and 7 payment traces, each containing authorization and acquisition spans.
One of those captures also included the SQL-wait and transaction-wait phases.
The other included a fault-control trace. Thus phase data survives the production
parser, but bounded selection does not capture every induced case. The last
disabled arm returned zero traces on its first three searches; a later bounded
check returned 8 traces (6 payment, 2 control). Visibility lag remains observable.

The first experiment used Tempo's V1 endpoint in the measurement script against
the product's V2 parser and therefore yielded no parsed spans. Its raw data is
retained, but its capture result is invalid. The experiment was repeated with
`/api/v2/traces/`; application behavior and acceptance thresholds were unchanged.
The tables above use only the corrected experiment, not pooled runs.

**Tempo memory:** 97 samples, zero sampling errors, at approximately 250 ms:

| Metric | Before | Sampled peak / after |
|---|---:|---:|
| Process RSS | 509.35 MiB | 590.75 MiB |
| Go heap allocated | 262.11 MiB | 320.84 MiB |

RSS grew about 81.4 MiB, exceeding the 64 MiB investigation threshold. It already
peaked at 579.36 MiB before capture searches. This shared Tempo started with data
and memory left by the first experiment, and export batching, GC and background
work are not isolated by these observations. The rise cannot be attributed solely
to new spans, and the small peak below 2 GiB cannot establish safety under sustained
testbed load. It is a reason to withhold broad rollout, not to increase resources.

**Decision:** the small instrumented phases have demonstrated diagnostic value.
Keep them opt-in and do not change capture limits, enable them across the lab,
or implement §32 on this evidence. A next step, if pursued, is an independently
registered isolated memory/load measurement plus real peer and Pod attribution;
alarm coverage remains a separate design. No new `RESOLVED` is claimed.

Reproduction requires the existing lab PostgreSQL and Tempo forwards:

```sh
kubectl -n observability port-forward service/tempo 13200:3200 14317:4317
.venv/bin/python -m scripts.payment_phase_feasibility --output .local/diagnostics/payment-phase-feasibility/repeat
```

Machine-readable observations, summary and provenance are under
`.local/diagnostics/payment-phase-feasibility/v2/`. The original V1 experiment is
retained in the parent directory. Tempo's experiment traces remain in its normal
retention; all local temporary database resources are cleaned up.
