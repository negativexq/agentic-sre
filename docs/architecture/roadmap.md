# Roadmap (2026-09-30)

Status: **WORKING DOCUMENT**, owner-maintained. It orders work and records what blocks what; it
changes no contract. Where a contract exists it is linked, and the contract wins.

Legend: **DONE**, **ACTIVE**, **NEXT**, **DECISION** (waiting for the owner), **PARKED**
(deliberately not started).

## 1. Direction

The engine's remaining limit is observability, not reasoning: the seen scenarios cannot supply
the exact execution, the first target-local effect, the propagation path or the recovery that a
verified mechanism needs (`m21-causal-closure-validation.md`, fault-execution measurements).
Work therefore goes in this order: a trustworthy boundary to the customer environment, a world we
can measure, and only then new causal rules.

The 35 seen ITBench scenarios are a **development regression set**. New causal semantics are not
derived from them; a new held-out set comes from our own instrumented testbed (§4).

## 2. A. Connector boundary

Contract: [`connector-boundary-contract.md`](connector-boundary-contract.md).

| # | Work | Status | Depends on |
|---|---|---|---|
| A0 | Boundary contract `connector.v1`; five owner decisions approved | DONE | |
| A1 | Logical separation: control-plane readers come from the connector client | DONE | A0 |
| A2 | Wire schema, in-process transport, refusal of lossy encodings | DONE | A1 |
| A3 | Migration gate: recorded real data survives the wire unchanged; direct-vs-wire epistemic digests identical (6 of 6 scenarios) | DONE | A2 |
| A4 | Alert path behind the connector: cursor-paged `read_alerts` and `read_changes`, `Gap`, heartbeat-derived `W`, local webhook receiver, change mirror (contract §10) | DONE in-process, opt-in via `SRE_CONNECTOR_STREAMS`; flipping the default stays a separate owner decision (existing tests pin the synchronous webhook reply) | A3 |
| A5 | Partial `list_events` listing (a failed namespace is skipped silently): contract amendment and fix | DECISION | |
| A6 | Live `query_traffic` and `query_traces` readers | PARKED | B |
| A7 | Physical separation: gRPC over mTLS, connector dials out, static certificates (contract §12, §13) | DONE (all suites pass over gRPC; the stream-mode default flip stays a separate decision; `uv lock` left to the owner) | A4 |
| A8 | `connectorctl preflight` with real probing, enrollment token, certificate issue and rotation, Helm | NEXT | A7 |
| A9 | Remote and multi-tenant operation, connector version compatibility | PARKED | A8 |

## 3. B. Testbed (an instrumented causal lab, not a benchmark)

| # | Work | Status | Depends on |
|---|---|---|---|
| B0 | Hygiene: `orders.created` is declared and re-created by a sidecar and Kafka keeps its data across container restarts (verified live); 88 unused Docker volumes removed and one kept | DONE | |
| B1 | `Timeline` ground truth (`testbed-ground-truth-contract.md`): manifest, journal, oracle, chain and scoring implemented and tested; scenarios and runner wiring come with B4 | DONE | |
| B2 | Recreate the lab cluster: Chaos Mesh 2.8.4 with containerd values, self-migrating Postgres, isolated control workload, gate passed (`testbed-lab-design.md` §13) | DONE | B1, A7 |
| B3 | Control plane outside the lab, its own Postgres; the Connector inside the lab dials out (`testbed-control-plane-design.md` §10) | DONE (all seven gate items pass, including a lab recreation with the history intact) | A7, B2 |
| B4 | Scenarios, repeats, dev and held-out split, frozen manifest, runner (`testbed-scenarios-design.md`) | ACTIVE: slices 1 and 2 run (3 repeats each); slice 3 (`config-or-rollout`) passed phase 0, repeats next; then `scheduled-recurring`, `competing-causes`, `negative-control` (needs the decoy amendment). No `HOLDOUT` tier yet | B1, B2, B3 |
| B5 | Frozen engine baseline: version and acceptance criteria fixed before any run | ACTIVE: done per slice (each manifest frozen with engine 2.1.0 and the acceptance thresholds before its runs); the whole-suite freeze follows the last family | B4 |
| B6 | Multi-cutoff recordings per run, to test timing stability against a known world | NEXT | B4 |

### Testbed results so far (2026-09-30, all `DEV`, engine 2.1.0)

| Slice | Fault | Valid runs | Cause and instance named | Execution witness | False strong authority / false `RESOLVED` |
|---|---|---|---|---|---|
| 1 | network delay on `payment-service` | 3/3 | 3/3 | 0/3 (a delay leaves no pod-level failure) | 0 / 0 |
| 2 | CPU stress on `order-service` | 3/3 | 3/3 | 1/3 (first real witness, checked against the truth) | 0 / 0 |
| 3 | `payment-service` environment change (rollout) | phase 0: 1/1 | 1/1 | 0 (no rollout rule yet, C5; expected) | 0 / 0 |

The engine names the right cause and instance every time and has never claimed false strong authority; strong
evidence is where it is weak, for reasons now on the C list (C2, C5, C9). Found and fixed on the way: a fault-execution
rule that credited a stale experiment, two scorer flaws that overstated recall, and harness isolation leaks plus an
unhealthy baseline (details in `testbed-scenarios-design.md`).

Two observability tiers are reported separately: production-realistic (what a customer
connector would supply) and instrumented (oracle channel, used only for grading). The engine
never receives ground truth.

## 4. C. Engine capabilities (after the testbed)

| # | Work | Status |
|---|---|---|
| C1 | Service-level effect relation: implement the specified relation (`m21-causal-semantics-contract.md`, "Service-level effect relation", deferred); parameters chosen on dev incidents, confirmed on held-out | NEXT |
| C2 | Observe the fault action (`spec.action`) and the first target-local effect time; bind resource-pressure findings (CPU throttling, memory) to the pod instance that held the name at that time, so a saturating fault's effect is an effect of the exact instance (testbed slice 2: the witness formed in 1 of 3 runs, only where a probe also failed) | NEXT |
| C3 | General fault-to-downstream-effect explanation (needs path evidence) | NEXT |
| C4 | Frontier closure by evidence | NEXT |
| C5 | New strong rules (rollout, config consumption, autoscaling) on the same witness and effect skeleton | NEXT |
| C6 | `RESOLVED`: every declared symptom covered by witnesses | NEXT |
| C7 | Capture history for live and replay sources (timing stability is `UNASSESSED` there) | NEXT |
| C9 | Evidence timing of a resolved incident: its window freezes at resolution, so an event that happened before the resolution but reached the control plane seconds later (the connector's event path lags about 15 s) is never seen, e.g. an experiment's `Recovered`. Decide the membership rule (event time versus arrival time, with or without a bounded arrival grace) in the causal semantics contract first | DECISION |
| C10 | **`STALE_CAUSAL_EVIDENCE` (P1, product defect).** An incident with no candidate that has evidence in its window still shows a leading actor chosen from evidence hours old. Measured on the 22 testbed databases: 23 of 122 incidents (19%) led with an actor whose every finding was more than an hour old, all of them `INSUFFICIENT_EVIDENCE` / `UNESTABLISHED` (e.g. `KafkaConsumerLag` at a load step led by an `order-worker` pod whose container failed 4.1 h earlier; the failure comes from the pod's current status, so no journal lookback bounds it). The epistemic state is right; the defect is that "outside the incident window" is only a ranking penalty (`ranking.py`), so the least-bad candidate still becomes `root_cause`, and the console shows it as "Possible causal actor", in the incident list and in the diagnosis notification. Fix to decide: withhold the leading actor when no candidate has evidence in the window (presentation), and/or exclude such candidates from leadership (engine, measured on the regression set first) | DECISION |
| C8 | Cost reduction for large onset sets (one representative per breakpoint) | PARKED |

## 5. D. Product surface

| # | Work | Status |
|---|---|---|
| D1 | UI notification: toast and badge on a new incident, a second one when the diagnosis lands; driven only by persisted state | DONE (verified in the browser against real alerts; `docs/ui/product-contract.md`, Notifications) | |
| D2 | Browser notification while the tab is closed | PARKED |
| D3 | Console shows timing stability, withheld authority, `instance_resolution` and status drivers | NEXT |
| D4 | Connect Cluster flow (token, install command, health, partial-coverage states) | NEXT (after A8) |

## 6. E. Benchmark suite (on top of the testbed)

| # | Work | Status |
|---|---|---|
| E1 | Metamorphic tests (identity, onset shift, renaming) and multi-cutoff tests | NEXT |
| E2 | Held-out set: own live scenarios with world-level ground truth (the same work as B4) | NEXT |
| E3 | Metrics: execution witness recall, effect-link recall, propagation-link recall, correct causal family, correct exact instance when knowable, false strong authority, false `RESOLVED`, time to resolution, evidence and read cost | NEXT |

## 7. F. Decisions and maintenance

| # | Item | Status |
|---|---|---|
| F1 | Engine version: `RCA_ENGINE_VERSION` stays 2.1.0 and a test pins it; the series is unreleased | DECISION |
| F2 | Name of the checkpoint that authorizes a push | DECISION |
| F3 | Old in-cluster `control-plane` deployment | DONE (moot: the recreated lab never deploys it; the control plane runs outside, B3) | |
| F4 | `.local` upkeep: compress `baseline-rivals*.json`, prune superseded run directories after checking references | NEXT |

## 8. Critical path

`A3 -> A4 -> B1 -> B0 -> B2 and A7 -> B3 -> B4 -> B5 -> C1 -> E3 -> C2 to C6`

Rationale: the boundary must be correct before anything is measured through it (A3, A4); the
world must be measurable before a rule is written for it (B); rules follow only where the
testbed can confirm them (C).

## 9. Risks

- **Stream semantics (A4).** Cursor, replay of missed events and `GAP` must behave the same
  in-process and over gRPC; this is the likeliest place for the two transports to diverge.
- **Tuning to examples.** Parameters chosen on one seen scenario fit that scenario (C1 was
  deferred for this reason). Dev and held-out are separated in B4 before the first run.
- **Resource limits.** Docker has about 10 GB; the full lab plus a second cluster does not
  fit, so the control plane runs outside the lab (B3).
- **`RESOLVED` stays at zero on the seen set.** Not a defect; the missing evidence kinds are
  measured in `m21-causal-closure-validation.md`.

## 10. Resource measurement for B2 (2026-09-30, read-only)

Measured on the running lab before any change. **Verdict: the planned lab fits.**

| Quantity | Measured |
|---|---|
| Docker VM | 8 CPUs, 9.7 GiB memory (host has 16 GiB) |
| Kind node container | 3.0 GiB of 9.7 GiB in use; about 0.3 CPU |
| Container working sets | 2.3 GiB: `sre-demo` 0.95, `observability` 0.76, `kube-system` 0.59 |
| Largest containers | Kafka 569 MiB, kube-apiserver 276, Tempo 190, Loki 164, Grafana 149, control plane 145 |
| Requested (memory) | `sre-demo` 1.4 GiB, `observability` 1.3 GiB, `kube-system` 0.3 GiB |
| Disk | 39 GB free on the host volume; the Kind node's `/var` uses 5.1 GB |

What B2 adds. Chaos Mesh figures are **requested** resources read from a rendering of chart 2.8.4
(`helm template`, nothing installed); actual use was not measured. The chart's defaults do not work on
this node: they assume the Docker runtime, while the node uses containerd
(`testbed-lab-design.md` §2).

| Addition | Requested memory |
|---|---|
| Chaos Mesh, one controller, no dashboard, no DNS server (the daemon sets no request) | 0.26 GiB |
| Chaos Mesh at chart defaults (three controllers, dashboard, DNS server) | 1.09 GiB |
| Connector pod inside the lab (estimate) | about 0.15 GiB |
| Oracle probe and an isolated control workload (estimate) | about 0.1 GiB |
| **Total in the Docker VM at the planned values** | **about 3.5 GiB of 9.7 GiB**, before test load |

Observations that shape B2:

- The observability stack (Prometheus, Loki, Tempo, Grafana, Alertmanager, OTel collector,
  kube-state-metrics) is **already running**; B2 does not add it again.
- The existing `sre-demo` workload already contains the edges the families need: a call edge
  (`order-service` to `payment-service`), an asynchronous edge (Kafka to `order-worker`) and a
  database. Direct, dependency, scheduled and config or rollout families need no new services;
  the negative control needs one small isolated workload.
- With the Connector inside the lab **dialling out** (A7), the observability services need no
  published ports, so the `extraPortMappings` that a Kind recreation would otherwise need are
  reduced to whatever the injector needs to drive workload traffic. Decide that in B2's design.
- The host is the tighter resource: 16 GiB total, about 52% free, with a large compressed set.
  Do not run the 35-scenario regression harness (four workers) at the same time as a testbed batch.
- Kind's chaos daemon needs the containerd runtime and socket settings; these are part of the
  Chaos Mesh install values, to be recorded in B2.

### Measured after the recreation (2026-09-30)

The recreated lab uses 2.0 GiB of the node's 9.7 GiB (container working sets 1.6 GiB); Chaos Mesh itself
uses 34 MiB against a 0.26 GiB request. The estimate above holds with a wide margin.
