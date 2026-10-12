# Kafka causal evidence: can the `kafka` question be closed safely?

2026-10-13, engine 2.14.0 at `60dce24`. Read-only development audit. No engine rule, contract text, lab setting or
stored record is changed. Every run here is development data. Ground truth labels results only.

## Question

In the current era, the `kafka` dependency question (`IS_THE_DEPENDENCY_AN_UNKNOWN_UPSTREAM_ROLE`, `CALLED`) is the
most common remaining frontier. This audit asks whether existing evidence can close it safely, without re-creating
the inferences withdrawn in §29.9 and §31.7:
- §29.9: healthy Kafka calls observed before the fault were read as health.
- §31.7: a cut sample of requests was read as a fact about every request.

The approach under test does not try to prove Kafka healthy. It asks whether the failures behind each alert are
positively observed at a stage the Kafka call cannot have produced.

## Population

Each incident's widest evidence window is recalculated on 2.14.0: 559 databases, 3,444 diagnoses, 0 errors.

| Set | Diagnoses |
|---|---:|
| One supported claim, strong, only frontier blocking | 473 |
| ... with no evidence-coverage record (eras before HOLDOUT19) | 428 |
| ... with coverage, no trace-read record (DEV19/19b, HOLDOUT19) | 14 |
| ... with coverage and trace reads (DEV22–23, HOLDOUT20–22): **the 31** | 31 |

The 31, by their open material questions:

| Open questions | Diagnoses |
|---|---:|
| `kafka` (`CALLED`) only | 22 |
| `kafka` (`CALLED`) + `payment-service` (`NOT_ON_EVERY_CLAIM_PATH`) | 6 |
| `kafka` + `payment-service`, both `CALLED` | 2 |
| `kafka` (`CALLED`) + `redis` (`TRACE_READ_FAILED`) | 1 |

None of the 31 has an unresolved rival. Each has exactly one alert:
- `OrderErrorRateHigh` (order 5xx rate): 18;
- `OrderDependencyLatencyHigh` (mean duration of order-service's calls to payment): 13.

## Resolution potential (oracle counterfactual, not a rule)

The `kafka` question is marked answered in every diagnosis (keyed on the name, for measurement only), and nothing else
changes. Over all 3,444 widest diagnoses, 0 errors, 0 root changes:
- **exactly 22 become `RESOLVED`**, the 22 above; no other diagnosis changes;
- all four `RESOLVED` conditions hold for them: one supported claim, which is strong; its symptoms covered; no
  unresolved rival; no remaining material frontier;
- the other 9 of the 31 stay `AMBIGUOUS` on the `payment-service` or `redis` question;
- **in all 22 the leader is the run's ground-truth cause.** The "negative/decoy" runs' decoys are `lab-control`
  actors; their cause is the payment Deployment, which is the leader;
- the 22 are 10 `OrderErrorRateHigh` and 12 `OrderDependencyLatencyHigh` diagnoses.

The 22 is an upper bound: it assumes a closure that answers every one of them.

## 1. Kafka's observed causal role

**Code path** (`workload/order_service/app.py`): create in PostgreSQL → `charge()` → update → `publish()` → response.
- An exception from `charge()` (HTTP error, timeout, refused connection) propagates and the request returns 500 before
  the update and before `publish()`.
- `DECLINED` still reaches `publish()`.

**Stored spans of the 31** (bounded reads, `TRUNCATED`; 4,056 order requests):
- every 5xx (410) has a failed payment client span and no Kafka span;
- every 201 (3,646) reached `kafka publish`;
- no other stage appears.

**Kafka publish duration:**
- median 0.4–1.0 ms before and inside the minute before onset;
- isolated maxima about 11 ms;
- order 201 requests take about 10–50 ms (median);
- no run shows a Kafka-driven rise.

Kafka is called by every successful request and by no failed one. "Called" and "contributed" are separable here, but
only through the stage at which each request failed, not through Kafka's health.

**`flush()` semantics** (`workload/common/adapters.py`):
- `flush()` without a timeout blocks until every queued message is delivered or fails (librdkafka's delivery timeout,
  300 s by default). The producer is one object per process, shared by every request thread, so a request's
  `kafka publish` span measures its wait for the **whole** queue, other requests' messages included, not its own
  message's delivery (correction, 2026-10-13);
- no `on_delivery` callback is set and `flush()`'s return value (messages still queued) is not read;
- a failed delivery raises nothing, the span ends without error and `kafka_messages_total{direction="produced"}`
  still increments;
- **delivery success and failure are not observed anywhere today.**

A Kafka fault therefore shows up in this workload mostly as request latency. It can still produce a 5xx: `produce()`
raises `BufferError` when the local queue is full and `KafkaException` on other local errors, and either propagates to
a 500 (correction, 2026-10-13: an earlier draft said none could). A delivery callback, once added, may run inside
another request's `flush()`, so its result must be bound to the message's own request, not to the active span.

## 2. Alert attribution

**Unsampled evidence that exists.**
- The SDK samples no trace (`ParentBased(AlwaysOn)`, no sampler set, no sampling processor in the collector). Only
  the engine's own trace read is bounded.
- Two log lines carry every failure:
  - the middleware logs every 5xx as `http.request error: … returned 5xx`, with `trace_id` and `request_id`;
  - `charge()` logs every payment failure as `dependency.request error: payment-service …`, with the same `trace_id`.
- Loki holds both with no retention limit. Tempo keeps 24 h; its pod was recreated, so these runs' traces are gone.

**Error alerts (`OrderErrorRateHigh`; all 18 of the 31, plus 3 Kafka-fault runs).** For each alert, every log line in
its pending interval plus the rule's 30 s range was read and the lines were joined by `trace_id`:
- **every 5xx request had a payment failure in the same trace** (1,653 of 1,653 over the 18; 46 of 46 in the Kafka
  runs);
- no 5xx without a payment failure; no payment failure without a 5xx; no other error line.

**Completeness.** Over [−5 min, +2 min] around each alert, the log lines were counted between consecutive samples of
the pod's own directly scraped 5xx counter:
- the counts are equal in **31 of 31** alerts (one replica each, no counter reset);
- both are written in the same process, in the same `finally` block. A loss common to both (a request killed before
  `finally`, a process crash) would leave them equal and incomplete. The equality shows consistency, not
  completeness of the request population;
- the OpenTelemetry series agree in 26 and differ by 2 to 7 in 5, from export lag.

Mixing the OpenTelemetry and Service-DNS sources (alarm attribution audit, finding 1) does not affect this test: it
states a fact of every 5xx request, not a share of the alert's value.

**Payment failure kinds** in the 10 `OrderErrorRateHigh` diagnoses among the 22: only timeouts and refused
connections. The 8 `config-c` diagnoses outside the 22 are dominated by `HTTP Error 500` from payment-service. For
those, payment-service logs `returned 500` with no reason, so its internal failure stage is unobserved.

**Latency alerts (`OrderDependencyLatencyHigh`; 12 of the 22).**
- The expression is the mean of `dependency_request_duration_seconds{dependency="payment-service"}`, recorded in
  `charge()`'s `finally`. The Kafka publish lies outside it.
- The only Kafka path into this quantity on the order side is contention in the shared process (`flush()` blocks a
  worker thread but releases the GIL); this is not observed.
- The label is a hard-coded string, not derived from the configured URL; it equals the declared host only by
  construction.

**The composite `OrderRequestLatencyHigh`** (order latency high and payment dependency latency low) is the Kafka-delay
signature. It is the only alert in the Kafka-alone runs. Neither argument above applies to it, so it stays open.

## 3. Negative controls in existing data

| Run | Alerts | Error-path test | Oracle effect |
|---|---|---|---|
| `dev23-kafka-delay` ×2 (Kafka alone) | `OrderRequestLatencyHigh`, lag | not applicable (latency) | none; stays `AMBIGUOUS` |
| `dev20`, `dev20b`, `dev21` (Kafka delay + payment loss) | error, dependency latency, lag | every 5xx at payment | none; a rival and a second supported claim remain |

- Every Kafka-fault run's ground-truth record is invalid (`missing recovery_at` or `missing target_effect_at`), and
  the combined runs list only the payment loss as cause. They are weak controls.
- No run has a Kafka fault that produces 5xx. Delivery failures are silent, but `produce()` exceptions are not; no
  scenario exercises them.

## 4. Why a rule cannot safely read this today

The facts above hold for these runs, but a name-free, versioned, replayable rule could not establish them:
1. **Stage identity is free text.**
   - The payment failure is identified only by the message `dependency.request error: payment-service` and code-location
     attributes.
   - The error log is emitted after the client span closes, so it carries the server span's id, not the client span's
     `server.address`.
   - Binding it to a Service would key on names (§3 I3).
2. **The engine's log read is bounded and not recorded completely.**
   - Error logs are read with a limit, and stored runs do not hold the complete log set.
   - A closure could not be replayed from the stored record.
   - Loki and Tempo run on `emptyDir`.
3. **Completeness depends on one replica.** The direct scrape goes through the Service DNS name. With two or more
   replicas it no longer identifies a pod, and the OpenTelemetry series lag and stall with the pod's network (alarm
   attribution audit, finding 3).
4. **The latency closure rests on a hard-coded metric label** and leaves process contention open.
5. **Payment declares Kafka.** payment-service's configuration declares the Kafka host (shared configuration), so
   §29.6's declarations do not exclude Kafka upstream of payment. That exclusion holds for the 10 error diagnoses
   only through the strong claim's own witness on the payment pod (timeouts and refusals at the payment call). It
   does not hold for HTTP 500s whose reason is unlogged.
6. **Kafka delivery outcome is unobserved.** A Kafka-raised 5xx (from `produce()`) is possible today but no scenario exercises it.

## 5. Smallest instrumentation that would make it observable

| Change | Proves | Cost |
|---|---|---|
| `on_delivery` callback; count `delivered` / `delivery_failed`; read `flush()`'s return; mark the producer span `ERROR` on failure | Kafka delivery result per message (independent of RCA; fixes `produced` and `KafkaConsumerLag`) | one callback per message; +2 series per (service, topic, instance) |
| Log dependency failures inside the client span, with `server.address` and `error.type` as attributes | per request, the failing call's declared host: binding through §29.6, not names | zero new lines; two attributes |
| `dependency` label from the configured peer host (`peer_host`), and the same histogram for Kafka | the latency alert's measured dependency bound by declaration; Kafka's own wait unsampled | +1 label value, ~17 series per instance |
| payment-service logs its own failure stage on 5xx (same structured form) | why payment failed, closing the HTTP 500 gap | zero cardinality |
| Scrape each pod directly, not the Service DNS name (lab decision already deferred) | an independent per-pod counter for completeness at any replica count | lab change, after HOLDOUT23 |

- **Not needed:** full trace capture or Tempo changes. Spans are already unsampled.
- **Needed for replay:** the engine would record a count-checked summary of the window's failures (per trace: stage,
  host), not every line. That is about 2,000 rows at most per incident here.

**What these observations could prove:**
- for the alert's window, every counted 5xx request failed at a call to a declared host of Service `P` before any call
  to Kafka;
- Kafka deliveries succeeded or failed, message by message.

**What they could not prove:**
- that Kafka was healthy;
- that Kafka did not add latency, except through its own measured wait;
- that a payment failure was not caused upstream by Kafka when payment's own stage is unobserved;
- completeness when every channel from the pod stalls.

## 6. Risks of a false `RESOLVED`

- A Kafka failure that surfaces as a 5xx (raised from `produce()`) would appear as a 5xx without a payment failure.
  The test fails, as it should, but this case has never been run.
- Overdetermination: Kafka and payment both failing, with the alert carrying only payment-stage 5xx. The closure is
  correct for that alert's symptom only. Any latency alert in the same incident must be answered on its own terms.
- A stalled log export read as "no other failures". Guarded only by an independent counter.
- Payment HTTP 500 with an unlogged reason: not closable.
- Closing on a mixed or composite alert: excluded; those stay open.

## Decision: `ADDITIONAL_INSTRUMENTATION_REQUIRED`

- **The evidence exists for the 22, but a rule cannot read it safely.**
  - The failure stage is observed per request, unsampled and complete for these runs.
  - It is observed only through free text and a hard-coded label, through reads the engine neither completes nor
    records, and with completeness provable only at one replica.
- **The expected gain is an upper bound.**
  - At most 22 development diagnoses (10 error, 12 latency), all led by the ground-truth cause.
  - Their closure needs the instrumentation above and a rule designed on development data.
  - The gain counts only after a new, pre-registered `HOLDOUT` on a later engine.
- **Required controls for that `HOLDOUT`:**
  - a Kafka fault that produces 5xx (`produce()` raising today; a delivery failure only if the application chooses to fail on it);
  - Kafka and payment failing together;
  - order-service with two replicas;
  - network loss on the order pod (export stall);
  - payment returning HTTP 500.
- **The Kafka delivery-result fix stands on its own** as an observability correction, whatever is decided about RCA.
- `NOT_CURRENTLY_FEASIBLE` is not chosen because the instrumentation is small and the control flow makes the stage
  observable. `EXISTING_EVIDENCE_SUFFICIENT` is not chosen for points 1–3 of §4.

## Limits

- Development data only.
- Loki and Prometheus are as of today; Tempo's traces for these runs are gone.
- One replica per run.
- The oracle keys on a name for measurement only; no rule may.
- The 428 older diagnoses lack the records any such rule would need and are not addressed.

## Reproduction

Outputs and the scratch scripts are under `.local/diagnostics/kafka-feasibility/`. The Loki and Prometheus reads need
`kubectl port-forward` to `localhost:13100` and `localhost:19090`.
