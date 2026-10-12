# Symptom explanation and causal materiality (design note)

2026-10-13. **Proposed; not approved, not implemented.** This note changes no engine behaviour, contract text, lab
setting or stored record. It makes no claim about any diagnosis's outcome. Measured facts it relies on are in
`kafka-causal-evidence-feasibility.md`. Everything below that is not a quotation of current code is a proposal for the
owner's decision.

## 1. The problem

Today a frontier question works like this:
- `derive_structural_frontier` derives a `dependency` alternative from every `calls` edge of a symptom workload.
- That edge comes from a host the workload declares (`_declared_calls`: environment and ConfigMap values naming a
  Service).
- `material_frontier` makes the alternative material for every admitted claim whose chain meets the affected workload.

Only two rules can then close a material `dependency` question:
- §29 A: the dependency is a hop of every material claim's own observed path;
- §29 C: the declared dependency was never called up to onset.

Both are statements about the dependency. Neither can use what is positively known about the symptom.

Two consequences follow:
1. **A dependency that is called stays open, whatever explains the symptom.** In the current era this keeps `kafka`
   open in all 31 diagnoses with a complete record (feasibility report).
2. **The question is asked asymmetrically.** Only symptom workloads' declarations are read. payment-service declares
   the same `kafka` and `redis` hosts (shared `envFrom`), but no question is ever asked about them, even when the
   symptom's failure is observed at payment.

The withdrawn rules show what closing a question from the dependency's side costs:
- §29.9 B read pre-fault calls as health;
- §31.7 read a cut sample as a fact about every request.

This note proposes the opposite direction: **explain the symptom positively, then decide which dependencies remain
material to that symptom, mechanism by mechanism.**

## 2. Three relations, kept apart

| Relation | Shown by | What it may decide |
|---|---|---|
| **Declared** | the workload's own configuration (`_declared_calls`, §29.6 hosts) | that a dependency is worth asking about: **discovery** |
| **Observed** | runtime calls from the caller (spans with `server.address` in the declared hosts, stage records) | that the caller did use it: a positive runtime relation, nothing about cause |
| **Material to a symptom** | a defined mechanism that could produce **this symptom**, assessed with its required evidence | whether the dependency blocks a decision **for this symptom** |

Declared and observed are inputs to materiality, never a verdict on it. Not observing a call never removes a declared
dependency (§3 I1). Being declared or called never makes a dependency material by itself.

## 3. The unit of assessment and its four states

Materiality is assessed for a triple: **dependency `D`, symptom episode `S`, mechanism class `M`**. The owner's four
states apply to each triple:

| State | Condition | Authority |
|---|---|---|
| `MATERIAL` | a trigger for `M` is positively observed, consistent with `S` in identity, time and effect (§6) | `D` is a real causal alternative for `S`; the question blocks |
| `UNKNOWN` | `M` is possible for `D` and `S`, but its required coverage is not proven | the question stays open (fail-closed) |
| `NOT_MATERIAL_TO_SYMPTOM` | `M` is defined, its required coverage is proven complete, and no trigger is found | lifts only the `(D, S, M)` part of the blocker |
| `STRUCTURAL_CANDIDATE` | `D`'s relation to `S` cannot yet be bound to any defined `M` | a research candidate; see §7 for whether it blocks |

The question "is `D` material to `S`" is then decided as follows:
- `MATERIAL` if any `M` is `MATERIAL`;
- otherwise `UNKNOWN` if any `M` is `UNKNOWN`;
- `NOT_MATERIAL_TO_SYMPTOM` only if **every** class of `D`'s role catalogue (§6) is `NOT_MATERIAL_TO_SYMPTOM`;
- a frontier question bound to several symptoms needs that verdict for each of them.

`NOT_MATERIAL_TO_SYMPTOM` is not health. It says: for this symptom's measurement, the mechanisms the contract defines
for this role were each observed with the coverage the contract requires, and none fired. The catalogue's completeness
is an explicit, versioned assumption of the contract. It is recorded in every such verdict, as §34 records the
journal's assumed completeness.

## 4. `SymptomExplanation`

This is a new, separate object. `CausalExplanation` keeps its claim-to-claim meaning (`explaining_claim` →
`explained_claim`, role transfer in `answer_frontier`) and is not extended.

```text
SymptomExplanation
  explanation_id, rule_id, rule_version, contract_version, engine_version

  symptom
    alert:        name, label-set digest, Service entity
    episode:      active_at, fired_at (= stored starts_at), pending interval
    measurement:  expression text + digest, form (RATE | RATIO | COMPOSITE | LAG | OTHER),
                  range, for, evaluation ticks checked,
                  measured quantity (REQUEST_OUTCOME_COUNT | OPERATION_DURATION | ...),
                  measured operation binding (server requests of a Service, or calls to a peer bound
                  through the caller's declared hosts), source series by job

  explaining_claim:  hypothesis id + the D1/strong witness ids it rests on (unchanged)

  mechanism
    kind:          e.g. REQUEST_TERMINATED_AT_PEER_OPERATION, PEER_OPERATION_DURATION
    actor:         EntityInstanceRef (UID) of the explaining actor
    caller:        instance → pod → UID, time-bounded (as in the alarm attribution audit)
    peer:          Service + declared host + selected pod UIDs over the episode

  execution_evidence        (positive, per counted symptom event)
    request ledger summary: trace_id, instance, terminal operation (peer host, error.type),
                            ordered operations with start, duration, outcome; counts by terminal stage

  coverage[]                (each independent of the evidence it checks)
    channel, quantity, interval, counted, explained, method, limits, result COMPLETE | INCOMPLETE | UNKNOWN

  direct[]                  per candidate D: verdict + reason (NOT_ON_TERMINAL_PATH, OUTSIDE_MEASUREMENT_BOUNDARY, ...)
  indirect[]                per (D, M): verdict + trigger evidence or coverage evidence + reason
  open_questions[]          question, D, M, reason code (COVERAGE_UNPROVEN, ORIGIN_UNOBSERVED, NO_MECHANISM_DEFINED, ...)

  evidence_ids, explained_evidence_ids, read_ids (the recorded reads it was built from)
```

**Coverage** means every instance of the measured quantity, over every evaluation tick's range in the pending
interval. It is counted on a channel independent of the evidence (for example a per-pod counter against the request
ledger).
- An aggregate over several alerts is never a coverage value.
- A series that stops advancing is `UNKNOWN`, never zero.

**Replay and authority:**
- an explanation is built only from reads recorded in the run, referenced by `read_ids`;
- it enters the epistemic digest;
- it adds a new answer state for a frontier question, scoped by symptom, for example `ANSWERED_NOT_MATERIAL_TO_SYMPTOM`
  with the symptom ids;
- D1, §29 A and C, and the withdrawn §29.9 and §31 are untouched.

## 5. Direct mechanisms

A direct mechanism is `D`'s own operation inside the measured events.

**Outcome-count symptoms** (for example a 5xx rate). `D` is `NOT_ON_TERMINAL_PATH` for `S` only when all of these
hold:
1. **Every counted event has a request ledger record.**
   - Coverage: ledger records equal the independent counter, per pod and per tick range.
2. **Each record names a terminal operation**, with peer host and `error.type`, whose peer is not `D`.
3. **`D`'s operations in that request, if any, are listed in the same record** with their outcome. None of them failed
   or exceeded the stage's bound before the terminal operation began, because an earlier slow `D` call could have
   consumed the request's deadline.

The ledger is **one record per request, complete by construction for that request**. That is what makes "no `D`
operation in this request" a positive statement about a whole record, rather than §31.7's inference from a cut
sample.

**Duration symptoms** (for example the mean duration of calls to a peer).
- `D` is `OUTSIDE_MEASUREMENT_BOUNDARY` when the measured operation is bound to a peer `P ≠ D` through the caller's
  declared hosts, and the measurement records only `P`'s operation.
- A hard-coded label equal to the host is not such a binding. The label must be derived from the configured peer.
- That `D` runs after `P` in the same request is a property of the ledger's ordered operations, not of the code.

A direct verdict never answers an indirect class.

## 6. Indirect mechanism catalogue (examples for decision, not approved)

Each class states its trigger, the coverage needed to call it absent, and how identity, time and effect bind a trigger
to `S`.
- **None of these signals is sufficient causation by itself.**
- **A trigger that fails any binding is `UNKNOWN`, not dismissed.**

| Class `M` | Trigger (positive) | Coverage required for `NOT_MATERIAL_TO_SYMPTOM` | Identity | Time | Effect |
|---|---|---|---|---|---|
| **Dependency's own verified fault** | a verified execution, change or not-`Ready` state on `D`'s selected pods (existing witness machinery) | `D`'s namespace/kinds continuously observed over baseline and episode (journal continuity, as in §26) | pod UIDs selected by `D`'s Service in the interval | overlaps the episode, begins before `fired_at` | a fault class that can change `D`'s operation outcome or duration |
| **Request-stage anomaly** | `D`'s stage durations or errors at the symptom's instances rise against their own baseline (unsampled stage metric) | `D`'s stage series per instance advancing over baseline and every tick range | same instances as the measured events | rise begins before or inside the pending interval | longer or failed `D` operations |
| **Shared-process contention** | at an instance with `D` stage anomaly, a saturated worker or connection pool, or §30 pressure | the pool gauges and the §30 read present and continuous for those instances | same pod UID as the measured operations | overlaps the pending interval | can lengthen or time out other operations in the process; cannot make the peer refuse a connection |
| **Resource pressure (§30)** on the symptom pod | §30 rise at that pod | the §30 read recorded for that pod | same pod UID | §30's own windows | shared, not specific to `D`: pressure **without** a `D` stage anomaly leaves `(D, contention)` `UNKNOWN`, not `MATERIAL` and not dismissed |
| **Origin of the peer's failure** | §7 | §7 | §7 | §7 | §7 |

- **Effect compatibility needs an approved table.** For example, `ECONNREFUSED` at `P`'s address is a fact about
  `P`'s listener, while a timeout can arise from the caller's side. Until that table is approved, any trigger is
  treated as compatible.
- **The catalogue is versioned.** Adding a class is a contract change.

## 7. Discovery versus materiality, and the depth bound

Discovery lists what can be investigated: every declared or observed dependency of the symptom workload and of the
explaining peer. Materiality decides which of them block a decision for `S`.

A **structural relation opens a blocking question only on the explanation's open edge.** This is proposed for the
contract, which must decide it.
1. **Hop 0, the symptom workload's declared dependencies:** blocking, as today, unless every class of §3 is
   `NOT_MATERIAL_TO_SYMPTOM`.
2. **Hop 1, the explaining peer `P`'s declared dependencies:** a question is opened only when the origin of `P`'s
   failure is not positively observed. Two observations close the origin:
   - the explaining claim's own witness on `P`'s pods (by UID, overlapping, effect-compatible); or
   - `P`'s own ledger naming its terminal operation.

   When the origin is observed, `P`'s other dependencies remain discovery candidates, visible and non-blocking for
   `S`.
3. **Each further hop** opens only if the previous hop's origin is unobserved.
4. **A fixed depth bound** (proposed: 2). Reaching it yields `UNKNOWN`, never exclusion.

Under this rule, today's asymmetry becomes stricter, not looser. A payment HTTP 500 whose internal stage is unrecorded
opens payment's declared `kafka` and `redis` questions as `UNKNOWN`. Today they are never asked.

`STRUCTURAL_CANDIDATE` keeps today's blocking behaviour wherever this rule does not place it off the open edge. No
declared dependency is removed from the frontier by a filter on declarations.

## 8. The two failure modes

**Failure mode 1: Kafka wrongly excluded, a false `RESOLVED`.**

| Situation | Where this design stops |
|---|---|
| Kafka delay, composite order-latency alert | no explanation of `S` by the payment claim exists (payment latency is low); question stays open |
| Kafka delay and payment loss, error alert | direct: Kafka not on the terminal path. Indirect: Kafka's verified fault (or its stage anomaly) is a trigger, so `MATERIAL` |
| Kafka degraded with no fault record | Kafka stage anomaly on the symptom instances is a trigger, so `MATERIAL`; stage series missing gives `UNKNOWN` |
| Log or metric export from the pod stalls | coverage `UNKNOWN`; nothing closes |
| Two or more replicas, only a Service-DNS counter | per-pod coverage unprovable, so `UNKNOWN` |
| Only bounded or sampled trace reads | coverage unprovable, so `UNKNOWN` (§31.7) |
| Healthy calls only before the fault (§29.9) | indirect classes need series advancing through every tick range of the pending interval; pre-fault health is never coverage |
| Kafka failure raised inside the request (5xx from `produce()`) | the ledger's terminal operation is Kafka, so `D` is on the path; nothing closes |

**Failure mode 2: recursive expansion, nothing resolves.**

| Situation | Where this design stops |
|---|---|
| Payment refused or timed out, the claim's witness is on payment's pods | hop 1 origin observed; payment's `kafka`/`redis` stay non-blocking candidates |
| Payment HTTP 500 with its stage recorded elsewhere | origin observed at hop 1; stops |
| Payment HTTP 500 with no recorded stage | hop 1 questions open as `UNKNOWN`. A correct refusal, not an endless one: the depth bound ends it |
| A shared ConfigMap declaring every host to every workload | declarations only enter through the open edge; a host declared by an off-path workload never blocks |

## 9. What would be enough, and what is missing today

This note does not claim the 22 candidate diagnoses would change. For each symptom type it states only what evidence
the design requires, and whether it exists today (feasibility report).

| Requirement | Error-count symptoms | Peer-duration symptoms | Exists today |
|---|---|---|---|
| Per-request ledger with ordered operations, peer host, `error.type` | yes | yes (ordering) | no: free-text log lines only |
| Independent per-pod counter of the measured quantity | yes | yes | only at one replica, through a Service-DNS scrape |
| Measurement bound to a peer through declared hosts | no | yes | no: hard-coded label |
| `D`'s per-instance stage duration and error series | yes (indirect) | yes (indirect) | no for Kafka |
| Pool/worker saturation per instance | yes (indirect) | yes (indirect) | no |
| §30 read for the symptom pod | yes (indirect) | yes (indirect) | yes |
| Kafka delivery outcome | for Kafka-raised failures | n/a | no |
| Recorded, replayable reads of all the above | yes | yes | no (logs read bounded, not recorded completely) |

## 10. Decisions for the owner

1. **The four states and their combination rule (§3),** including the catalogue-completeness assumption carried in
   every negative verdict.
2. **The `SymptomExplanation` fields (§4)** and its new answer state's scope.
3. **The indirect catalogue (§6):** which classes, their trigger and coverage definitions, and the effect-compatibility
   table.
4. **The open-edge rule and depth bound (§7)**, and the resulting stricter treatment of an explaining peer's
   dependencies.
5. **Coverage channels (§4, §9):** per-pod independent counters, which needs the lab's scrape change after HOLDOUT23.
6. **Instrumentation order:** the request ledger and stage metrics before any rule. The Kafka delivery fix is a
   separate PR, independent of this design.

## 11. Before any contract text

1. Specify and implement the instrumentation of §9 on development scenarios only.
2. Measure on new `DEV` runs, read-only:
   - ledger completeness against the per-pod counters;
   - stage-series continuity under network loss on the caller pod;
   - each catalogue class's trigger rate in runs with and without a dependency fault.
3. Then draft contract text with its own paired replay.
4. Then pre-register a `HOLDOUT` on that engine with at least:
   - a dependency fault that produces errors;
   - two dependencies failing together;
   - two replicas;
   - an export stall;
   - a peer HTTP 500 with and without a recorded stage.
