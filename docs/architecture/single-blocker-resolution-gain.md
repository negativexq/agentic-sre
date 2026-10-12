# Resolution gain: the single-blocker candidates

2026-10-12, engine 2.11.0, read-only development audit. No production coverage or
closure rule is changed. Script: `scripts/single_blocker_gain_audit.py`.

## Population

The earlier recent-era measurement identified 23 diagnoses with a strong witness
and one blocker **category**: 20 frontier-only and 3 coverage-only. Frontier-only
does not mean only one open question. Their frozen run IDs were recalculated
directly. Three differ from the event-ID tie-broken widest-window selection in
the 3,444-window census, so they were not silently joined or substituted.
The 23 are from 14 testbed runs and are all development data.

## Where there is a concrete candidate

The three coverage-only diagnoses are from one unscored DEV run,
`dev19-two-replicas/direct-stress-payment/0`:

- `PaymentDbQueryLatencyHigh`
- `PaymentRequestLatencyHigh`
- `HighRequestLatency`

Both payment Pods have separate fired execution/effect witnesses carried by the
same supported StressChaos claim. Every declared Pod and Deployment is already
covered. Only `Service/payment-service` remains uncovered.

§28 covers a Service through a Pod witness only when that Pod is the sole ready
server. It does not combine witnesses for all replicas. This is an explicit
contract boundary, not an accidental omission: testbed design §35 pre-registered
no §28 Service coverage and no resolution through that route at two replicas.

The two execution intervals overlap from 20:34:29 to 20:36:17 UTC. Each Pod has
one recorded UID. Pod and Service observation is proven continuous over that
interval by the frozen record (the two-hour aggregate summary is UNKNOWN because
the stream started later; `continuously_observed` proves the shorter interval).
The Service selector is unchanged; the retained Pod history before the end
contains exactly those two matching identities, with no selector write during
the interval. These facts were checked, not inferred from an empty trace search.

An illustrative audit override adds Service coverage when all selected Pods have
their own strong witnesses, a common execution interval, one observed UID each,
and Pod/Service observation continuity. It makes all three diagnoses `RESOLVED`,
also with C's negative authority withheld. Removing the scoped override restores
the baseline. This adds no execution witness and does not alter root support.

The 3,444-window census has six non-resolved diagnoses with multiple Pod witnesses
in the same support record, across two runs. All six were separately recalculated:
the override resolves the same three; the other three retain their other blockers.
There is no demonstrated broad gain from this route beyond one DEV run.

**This is a counterfactual candidate, not an implemented or validated +3.**
The prototype uses the frozen selected set; historical membership was checked
separately for the positive run. It is not a production algorithm for proving the
complete serving population over time. Alarm attribution and consistency with
the established coverage meaning must be explicit in a new contract.

## What a safe new design would require

Propose a separate versioned collective-coverage rule; do not quietly relax the
current §28 or delete Service symptoms. It would require:

1. Positive observation of the complete relevant Service membership and readiness
   population over the effect interval, including arrivals, removals and UID changes.
2. A same-claim, exact-instance execution/effect witness for every possible serving
   Pod; no uncovered ready replica can be skipped.
3. Compatible episode and intervals. A group of unrelated effects or separate
   independent causes cannot establish one claim's Service coverage.
4. Existing rival/frontier and remaining symptom-coverage conditions unchanged.
5. Explicit semantics for the Service alarm being covered: all-member coverage is
   not an automatic proof of a sampled latency mean or DB metric threshold.

Before implementation, register independent controls: all replicas affected by
one cause; one affected and one healthy; an extra arriving replica; missing
membership/readiness history; reused Pod name; non-overlapping effects; separate
causes; and unrelated Service alert behavior. The first should be a positive
control, the remainder must not gain unsupported Service coverage or resolution.
An unseen testbed run must validate it. No threshold is fitted to the three cases.

The prototype rejects additional coverage when a witness is removed, an extra
unwitnessed Pod is added or continuity proof is absent. Removing one Pod's status
revealed a separate existing-rule issue: `_sole_ready` can treat that other Pod's
missing observations as not ready, letting the old single-Pod rule grant coverage.
The collective addition supplies no new coverage in that control, but the overall
legacy fallback is not fail-closed. No real false resolution from that issue is
shown here. A safe coverage design must close that unknown-readiness boundary;
the negative control is not reported as a successful rejection by the whole engine.

## Why the other 20 are not an immediate gain

With C's negative closure withheld:

| Frozen evidence situation | Diagnoses | Remaining frontier |
|---|---:|---|
| Read records present | 12 | Kafka `CALLED` and Redis `TRACE_COVERAGE_NOT_PROVEN` |
| A failed Redis read | 1 | Kafka `CALLED` and Redis `TRACE_READ_FAILED` |
| Legacy read records absent | 5 | Kafka and Redis roles undetermined |
| Legacy read records absent, config written in window | 2 | Kafka, Redis and `WRITTEN_IN_WINDOW` |

The sum is 20. Reading healthy samples or completing a bounded search cannot
close those questions under the proposed C boundary. Adding metadata to old runs
would not create missing observations. New capture and a separately validated
closure mechanism would be necessary. §31 and healthy-call rule B remain withdrawn.

## Recommendation and reproducibility

For a bounded resolution-specific investment, design and independently measure
collective replica coverage first. It uses existing observations and adds no
Tempo load. The measurable candidate is three diagnoses in one run, so it is a
small capability extension, not a claim of major resolution growth. Withhold a
large Kafka/capture investment until its evidence requirements and expected gain
are demonstrated. Payment phase telemetry remains useful diagnostic groundwork,
not a measured resolution gain.

Local inputs and outputs are retained under `.local/diagnostics/c-trace-coverage/`:
`single_blockers.json`, `single_gain.json`, `multi_pod_jobs.json`, and
`multi_pod_gain.json`. These are audit artifacts, not engine records.

```sh
.venv/bin/python -m scripts.single_blocker_gain_audit .local/diagnostics/c-trace-coverage/single_blockers.json --output .local/diagnostics/c-trace-coverage/single_gain.json
.venv/bin/python -m scripts.single_blocker_gain_audit .local/diagnostics/c-trace-coverage/multi_pod_jobs.json --output .local/diagnostics/c-trace-coverage/multi_pod_gain.json
```

Normal replay remains version-strict. Neither stored inputs nor stored diagnoses
were modified; database sessions use `default_transaction_read_only=on`.
