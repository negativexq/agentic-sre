# Symptom explanation architecture (design note, revision 2)

2026-10-13. **Proposed; not approved, not implemented.**
- This note changes no engine behaviour, contract text, lab setting or stored record, and no diagnosis's outcome.
- The measured facts it relies on are in `kafka-causal-evidence-feasibility.md`.

**Revision 2** follows the owner's review of revision 1.
- Revision 1 built the architecture around excluding dependencies: "every catalogued mechanism clean, so not
  material", plus a fixed two-hop stop.
- Both are withdrawn here. The architecture now separates three things:
  - explaining a symptom;
  - establishing the origin of what explains it;
  - adjudicating the alternatives.

## 1. Fixed principles (owner decisions, 2026-10-13)

- **A. A symptom explanation is not root-cause verification.**
  - `SYMPTOM_EXPLAINED` never produces `RESOLVED` by itself.
  - D1, strong witnesses and the four `RESOLVED` conditions are unchanged.
- **B. Exhausting a catalogue is not negative evidence.**
  - No trigger in any known mechanism does not make a dependency "not material".
  - A negative verdict exists only for **one relation**, proven incompatible with the symptom by time, identity or
    control flow.
  - Missing observation, an incomplete catalogue and unknown indirect effects stay `UNKNOWN`.
- **C. Failure location is not causal origin.**
  - Knowing a service's terminal operation does not explain why it failed.
  - Upstream investigation stops only on positive causal-origin evidence. A budget may end it, but only as
    `OPEN / INVESTIGATION_BUDGET_EXHAUSTED`, never as exclusion.
- **D. `SymptomExplanation` is a separate model.**
  - `CausalExplanation` keeps its claim-to-claim meaning.
  - Each explanation is bound to one claim and one symptom episode. An explanation for one claim never answers another
    claim's question.
- **E. Coverage is more than equal counters.**
  - It must consider:
    - request lifecycle and terminal records;
    - export loss and pod restarts;
    - counter resets and UID changes;
    - loss common to the compared sources.
  - Without sufficient proof it is `UNKNOWN`.
- **F. Kafka's shared producer is part of the design.**
  - `flush()` waits for the process's whole queue.
  - `on_delivery` may run inside another request's `flush()`.
  - A delivery result binds to the message's originating request, not to the active span.
  - `produce()` raises `BufferError` and `KafkaException` synchronously.
  - The delivery-telemetry fix does not change HTTP behaviour.
- **G. The first version is narrow.** It covers HTTP failure propagation, dependency-operation duration attribution,
  and coverage and unknown-reason reporting. It contains no Kafka closure, no dependency exclusion and no automatic
  `RESOLVED`.
- **H. Testability is defined before implementation** (§9).
- **I. Replay and digest are mandatory.**
  - Every piece of evidence with decision authority is bound to versioned, immutable recorded reads.
  - Normal replay never re-queries Loki or Tempo.
- **J. Rollout is staged.** First explanatory-only output with offline comparison; any frontier authority comes later,
  narrowly, under its own versioned contract.

**Standing rule of this note:** an epistemic assumption that is not approved grants no authority. Its place in the
model is `UNKNOWN`.

## 2. Four layers

```text
INCIDENT
  └─ A. SYMPTOM DEFINITION    alert expression, measurement boundary, episode, affected instances
       └─ B. EVIDENCE VALIDATION  positive observations, identity, coverage, provenance
            └─ C. SYMPTOM EXPLANATION  observed mechanism, failure location, explaining claim, unexplained part
                 └─ D. CAUSAL ADJUDICATION  initiating cause, alternatives, unresolved origins, authority
```

Layers A–C produce evidence. Only layer D, the existing deterministic engine, decides. In the first version layer D
reads nothing from A–C.

### A. Symptom definition

Each alert is defined by its own expression, not by its name:

| Alert | Measures |
|---|---|
| `OrderErrorRateHigh` | order-service 5xx rate |
| `OrderDependencyLatencyHigh` | mean duration of order-service's calls to its payment peer |
| `OrderRequestLatencyHigh` | composite: order request mean high and payment call mean low |
| `KafkaConsumerLag` | a difference of application counters, not broker offset lag |

- **Mixed sources stay separate.** Where an expression sums sources (the OpenTelemetry series and the Service-DNS
  scrape), each source is a separate term with its own identity and coverage. An explanation covers a term, never the
  sum by assumption.
- **Forms outside v1 are only defined.** Composite and lag forms are recorded in v1 and not explained.

### B. Evidence validation

A record gains a place in an explanation only after four checks:
- **identity:** `service.instance.id` → pod → `k8s.pod.uid`, time-bounded through the journal as in §34;
- **time;**
- **integrity:** the record is the one the source emitted;
- **provenance:** the recorded read it came from.

`trace_id` presence or matching counters certify nothing on their own (§4, coverage certificate).

### C. Symptom explanation

A `SymptomExplanation` states one bounded fact: **this symptom episode's measured quantity is accounted for by this
observed mechanism, to this coverage.** For example: the order 5xx in this episode are the requests whose terminal
operation was a failed call to the payment peer, and all of them are accounted for.

It never states that the mechanism's actor is the incident's cause.

### D. Causal adjudication

This is the current engine. In later stages it may read explanations as additional evidence, never as replacements
for witnesses.

## 3. Decision semantics

These verdicts are distinct and never substituted for one another:

| Verdict | Meaning | Implies |
|---|---|---|
| `FAILURE_LOCATION_OBSERVED` | the terminal failing operation of counted events is observed (caller, operation, peer, `error.type`) | nothing about why it failed |
| `MECHANISM_OBSERVED` | a mechanism's execution is observed (for example a witness, or the ledger's failing operations) | nothing about the symptom |
| `SYMPTOM_EXPLAINED` | the mechanism accounts for the episode's measured quantity, with a `COMPLETE` coverage certificate | nothing about origin or alternatives |
| `CAUSAL_ORIGIN_VERIFIED` | the explaining mechanism's origin is tied to an actor by an approved origin rule (§5) | not approved in v1; always `UNKNOWN` |
| `ALTERNATIVE_EXCLUDED_WITH_PROOF` | one named relation of one candidate is proven incompatible with the symptom (§4.4) | only that relation |
| `UNKNOWN` | evidence or coverage insufficient, with a reason code | — |

## 4. Data model (v1 shapes; field names illustrative)

### 4.1 `SymptomEpisode` and `MeasurementBoundary`

```text
SymptomEpisode
  alert name, label-set digest, Service entity
  active_at, fired_at (stored starts_at), pending interval, evaluation ticks
  measurement: MeasurementBoundary

MeasurementBoundary
  expression text + digest, form (RATE | RATIO | COMPOSITE | LAG | OTHER), range, for
  quantity: REQUEST_OUTCOME_COUNT | OPERATION_DURATION | UNSUPPORTED
  operation binding: SERVER requests of a Service | CLIENT calls to a peer bound through the caller's declared hosts
                     (a label that is not derived from configuration is not a binding: UNKNOWN)
  terms[]: one per source (job, selector), each with its own identity and coverage
```

### 4.2 `ExecutionRecord` (one per request; instrumentation in §8)

```text
ExecutionRecord
  record id, trace_id, request id, service.instance.id, k8s.pod.uid, route
  started_at, finished_at, terminal: HTTP status | PROCESS_ABORTED
  operations[] (ordered): kind (HTTP_CLIENT | DB | MESSAGING_PUBLISH | ...), peer host (configured), started_at,
                          duration, outcome, error.type
  terminal_operation: index into operations, or NONE
```

- A start record and a terminal record are written separately.
- A start without a terminal is a lifecycle gap. It is never a success.

### 4.3 `CoverageCertificate`

A data model, not a guarantee. Each check is recorded with its result; any check that cannot be shown yields
`UNKNOWN`.

```text
CoverageCertificate
  population: which events must be accounted for (the term's counted quantity, per pod, per tick range)
  channels[]: each source compared, with its independence class (SAME_PROCESS | SAME_EXPORTER | INDEPENDENT)
  lifecycle: starts vs terminals per pod; open starts at interval end
  continuity: every series advancing through every tick range; export gaps
  restarts: process_start_time changes, pod restarts, UID changes over the interval
  resets: counter resets (intervals with a reset are not compared)
  common-mode: a loss that would remove an event from every compared channel at once
  result: COMPLETE | INCOMPLETE | UNKNOWN, with reasons
```

**`COMPLETE` requires** at least one channel independent of the evidence's process and exporter, plus lifecycle
balance.

The feasibility audit's 31/31 log-to-counter equality is `UNKNOWN` under this model:
- both channels are written in the same process, in the same `finally` block;
- so a request killed before `finally` vanishes from both.

### 4.4 `SymptomExplanation` and relation assessments

```text
SymptomExplanation
  explanation id, rule id + version, contract version, engine version
  episode: SymptomEpisode
  claim binding: hypothesis id + the witness ids it rests on   (one claim, one episode)
  verdicts: FAILURE_LOCATION_OBSERVED | MECHANISM_OBSERVED | SYMPTOM_EXPLAINED  (each with evidence)
  mechanism: kind (HTTP_FAILURE_PROPAGATION | DEPENDENCY_OPERATION_DURATION), caller instance → pod UID,
             peer Service + configured host + selected pod UIDs over the episode
  accounting: counted, accounted-for, by terminal operation; unexplained remainder
  coverage: CoverageCertificate
  causal origin: UNKNOWN (v1), reason ORIGIN_RULE_NOT_APPROVED
  relations[]: RelationAssessment
  open questions[]: (candidate, relation, reason code)
  provenance: evidence ids, recorded read ids, record digests

RelationAssessment
  candidate (a dependency or actor), relation kind, verdict, proof or reason
```

**Relation kinds** are assessed separately for each candidate:
- `DIRECT_IN_REQUEST`: the candidate's operation inside the counted requests;
- `DIRECT_IN_MEASUREMENT`: the candidate's operation inside the measured quantity;
- `INDIRECT_SHARED_RESOURCE`: contention in the same process;
- `INDIRECT_OWN_FAULT`: a fault on the candidate itself;
- `UPSTREAM_OF_PEER`: the candidate behind the explaining peer.

The only proof forms for `ALTERNATIVE_EXCLUDED_WITH_PROOF`, all requiring approval before use:

| Relation | Proof of incompatibility |
|---|---|
| `DIRECT_IN_MEASUREMENT` | the measured quantity is bound to operations whose configured peer is not the candidate (binding through configuration, not a label string) |
| `DIRECT_IN_REQUEST` | within a complete record, the candidate's operations all begin after the terminal operation ended, or are absent from a record that is complete for that request; plus a `COMPLETE` certificate over the population |
| any | the candidate's earliest possible effect begins after the episode's last tick, from its positively observed onset |

**Indirect relations have no exclusion form in v1:**
- every indirect relation is `UNKNOWN`, unless a positive trigger makes it an **open rival mechanism**;
- "no anomaly observed" never excludes.

## 5. Failure location, causal origin and investigation budget

```text
Order 500 ← failed call to Payment ← Payment 500 ← Payment's call to Kafka timed out ← ? (broker, network,
client resources, something unrelated)
```

Each arrow until the last is a **failure location**. None of them is an origin.
- **v1 records origin as `UNKNOWN`** for every explanation. Its reason is `ORIGIN_RULE_NOT_APPROVED`.
- **A later origin rule** would have to say which witness, bound to which pods by UID, in what time relation and with
  what effect compatibility, establishes the origin of a failure location. Until approved, no stop is granted.

Investigation proceeds along failure locations: the explaining peer, then that peer's own terminal operation, and so
on. It stops in one of two ways:
- **origin verified:** not available in v1;
- **budget exhausted:** recorded as `OPEN / INVESTIGATION_BUDGET_EXHAUSTED`, with what was and was not read.

The budget counts edges, reads and time. It is an engineering limit, never a causal conclusion. There is no fixed hop
bound.

**Discovery** still lists every declared and observed dependency along the explored locations. That includes the
explaining peer's own declarations (payment declares `kafka` and `redis`), which the current frontier never asks
about. They appear as `UPSTREAM_OF_PEER` relations with verdict `UNKNOWN`.

## 6. Kafka producer semantics (design inputs, independent of RCA authority)

- **One producer per process** (`LazyKafkaEventPublisher` in `create_app`), shared by every request thread.
  - `flush()` waits for the whole queue, so a request's `kafka publish` duration is its wait for every queued message,
    its own included.
  - The ledger records it as `MESSAGING_PUBLISH` with `queue_depth_at_flush`. It is never one message's delivery
    latency.
- **Delivery outcome.** An `on_delivery` callback is created at `produce()` time, closing over the message's own
  correlation:
  - record id, `trace_id`, operation index.
  
  The callback may run in any thread's `flush()` or `poll()`. It records the outcome against that captured
  correlation, never against the active span.
- **Synchronous errors.** `produce()` raises `BufferError` (queue full) and `KafkaException` (local errors), and the
  request ends 500. The ledger names `MESSAGING_PUBLISH` as the terminal operation. This is a current Kafka-raised 5xx
  path that no scenario exercises.
- **No silent behaviour change.** Recording delivery failures does not make the request fail. Whether the application
  should fail on them is a product decision outside this design.
- **Lag.** Broker offset lag (log end offset against the consumer group's position) is a separate measurement from the
  application counter difference. It needs a broker-side exporter, which is a lab change after HOLDOUT23.

## 7. Version 1 scope

- **In:**
  - HTTP failure propagation (`REQUEST_OUTCOME_COUNT` symptoms);
  - dependency-operation duration attribution (`OPERATION_DURATION` bound through configuration);
  - `CoverageCertificate`;
  - `RelationAssessment` with `UNKNOWN` reasons and the two direct proof forms;
  - recorded reads and digest participation, with the explanation carrying **no authority**.
- **Out:**
  - Kafka-specific closure, dependency exclusion and any frontier answer state;
  - any `RESOLVED` change;
  - origin verification;
  - composite and lag symptoms;
  - indirect-relation exclusion.
- **Engine behaviour:** v1 runs inside diagnosis, and its output is stored alongside the diagnosis. Layer D does not
  read it. Every existing decision and digest field is unchanged; whether the explanation enters a new, separate
  digest is decision 3 in §11.

## 8. First instrumentation boundary

| Change | Where | Standard | Feeds |
|---|---|---|---|
| `ExecutionRecord` start and terminal records | workload middleware and clients | OpenTelemetry log records with `trace_id`, `span_id`, `service.instance.id`, `k8s.pod.uid`; operation attributes `server.address`, `error.type` | §4.2 |
| Dependency failure logged inside the client span | `HttpPaymentGateway`, DB, Kafka adapters | HTTP and messaging semantic conventions | §4.2 |
| Duration metric's peer label from the configured URL; same histogram for messaging | telemetry runtime | — | §4.1 binding |
| Kafka `on_delivery` with captured correlation; delivered/failed counters; `flush()` remainder recorded | `KafkaEventPublisher` (**separate PR**) | messaging semantic conventions | §6 |
| Per-pod direct scrape (not Service DNS) | lab, after HOLDOUT23 | — | §4.3 independent channel |
| Recording the reads of all the above into the run | engine evidence capture | — | principle I |

Whether a channel independent of the workload process exists is an open requirement (§11, decision 5). Candidates
are the caller's own ledger and a proxy or ingress count. Until one exists, certificates are `UNKNOWN`.

## 9. Negative-control matrix (pre-registered expectations for v1)

**Run expectations:**
- **v1 output** is what the explanatory layer must report.
- **A later closure** is what any stage-6 authority must do; it is listed now so the rule cannot be fitted to results.

| Scenario | v1 output | A later closure must |
|---|---|---|
| payment timeout, Kafka healthy | `SYMPTOM_EXPLAINED` only with a `COMPLETE` certificate; origin `UNKNOWN`; Kafka relations `UNKNOWN` except proven direct ones | close at most the proven direct relations |
| Kafka-only latency (composite alert) | `UNSUPPORTED` form; no explanation | not close |
| payment and Kafka failing together | payment explains the 5xx; Kafka `INDIRECT_OWN_FAULT` is an open rival | not close |
| Kafka `produce()` exception | terminal operation `MESSAGING_PUBLISH`; Kafka `DIRECT_IN_REQUEST` is a fact, not excluded | not close |
| Kafka delivery failure | recorded against the originating request; HTTP unchanged; no symptom unless the app fails | not close |
| payment 500 caused by its own Kafka call | failure location at payment, then at payment's Kafka call; origin `UNKNOWN` | not close |
| two replicas | per-pod certificate; a Service-DNS term is `UNKNOWN` | not close on that term |
| pod restart inside the episode | restart recorded; certificate `UNKNOWN` unless lifecycle balances across it | not close |
| counter reset | reset intervals excluded; remainder must still balance | not close otherwise |
| exporter stall (network loss on the caller pod) | continuity fails, so `UNKNOWN` | not close |
| mixed alert sources | each term separate; an unattributable term is `UNKNOWN` | not close on it |
| an unknown mechanism present (a fault outside the catalogue) | relations `UNKNOWN` | not close: **the negative closure is refused** |

## 10. Stages

| Stage | Work | Engine behaviour | Exit criterion |
|---|---|---|---|
| 1 | fix this note's semantics (owner approval) | unchanged | decisions of §11 taken |
| 2 | Kafka delivery telemetry; structured dependency telemetry | unchanged | unit tests; deploy only after HOLDOUT23 |
| 3 | `ExecutionRecord`, coverage channels, recorded reads | unchanged | ledger balance measured on new `DEV` runs |
| 4 | explanatory-only `SymptomExplanation` | `RESOLVED` unchanged | offline comparison over §9's matrix on new `DEV` runs |
| 5 | relation assessments for indirect mechanisms (triggers only) | explanation and open questions only | trigger rates with and without dependency faults |
| 6 | a narrow, evidence-backed frontier closure | its own versioned contract, paired replay, unseen `HOLDOUT` | owner approval |

No stage presumes the 22 candidate diagnoses change. Their expected v1 output is an explanation, where coverage allows,
with every Kafka relation either proven direct-incompatible or `UNKNOWN`.

## 11. Unapproved assumptions (fixed at `UNKNOWN` until decided)

1. **Origin rule:** what establishes `CAUSAL_ORIGIN_VERIFIED`.
2. **Effect-compatibility table:** for example, whether `ECONNREFUSED` at a peer can arise from the caller's side.
3. **Digest:** whether explanation records enter a separate digest from v1 or only from stage 6.
   **Decided 2026-10-13 (owner):** not in v1. Explanations are stored beside the diagnosis and enter no digest; a
   separate digest comes with authority (stage 6).
4. **Direct proof forms of §4.4:** their exact conditions.
5. **Independence classes of coverage channels,** and whether any current channel is `INDEPENDENT`.
   **Decided 2026-10-13 (owner):** only the testbed caller's ledger (`WorkloadDriver`, its own process and host,
   outside the workload's telemetry path) is `INDEPENDENT`. Loki, Tempo and the workload's own counters are
   `SAME_PROCESS` or `SAME_EXPORTER`. Where no caller ledger exists the certificate is `UNKNOWN`.

Decisions 1, 2, 4, 6 and 7 stay `UNKNOWN` in v1: origin is always `UNKNOWN`, and nothing is excluded.
6. **Journal completeness of the pod population:** assumed in §33–§34, not proven here.
7. **Investigation budget values.**
