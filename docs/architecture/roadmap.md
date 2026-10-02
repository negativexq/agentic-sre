# Roadmap (2026-10-02)

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
| A10 | Watch-driven change stream (contract §15, `connector-watch-design.md`): per-scope LIST then WATCH, `Gap(RESOURCE_VERSION_EXPIRED)` and a snapshot on an expired version, observed and inferred deletions, the control plane journaling as changes arrive, wire `connector.v2`. Change-to-journal median 21.5 s → 1.2 s (p90 27.1 s → 1.9 s); API requests about 6.5 times fewer; API outage, connector restart and an expired version recovered in the lab (§15.1, §15.2) | DONE (three-hour soak, §15.5: 0 failures, 35 scope gaps on quiet Event scopes, 0 global snapshots, end-to-end p50 1.19 s / p90 2.01 s) | A7 |
| A11 | Synthetic bookmarks on quiet Event scopes (contract §15.6): a `limit=1` LIST every 120 s while the scope's watch is open, adopted only by the same watch at a normal end | DONE (owner-approved; soak: 0 Event version expiries in 137 min, against 35 in 180 before) | A10 |
| A12 | Long-run findings on the connector path: a replaced alert occurrence ends at the new `startsAt` (`incident-episode-contract.md` §9); chaos kinds listed in every journaled namespace, a 403 or 404 outside the chaos namespaces skipped with one warning instead of an endless relist; lab RBAC reads `chaos-mesh.org` (`chaos-objects-design.md`) | DONE (10-hour product run found them; a 2-hour run after the fixes: 0 stuck incidents, 0 Event gaps, 4/4 experiments journaled) | A10 |
| A8 | `connectorctl preflight` with real probing, enrollment token, certificate issue and rotation, Helm | NEXT | A7 |
| A9 | Remote and multi-tenant operation, connector version compatibility | PARKED | A8 |

## 3. B. Testbed (an instrumented causal lab, not a benchmark)

| # | Work | Status | Depends on |
|---|---|---|---|
| B0 | Hygiene: `orders.created` is declared and re-created by a sidecar and Kafka keeps its data across container restarts (verified live); 88 unused Docker volumes removed and one kept | DONE | |
| B1 | `Timeline` ground truth (`testbed-ground-truth-contract.md`): manifest, journal, oracle, chain and scoring implemented and tested; scenarios and runner wiring come with B4 | DONE | |
| B2 | Recreate the lab cluster: Chaos Mesh 2.8.4 with containerd values, self-migrating Postgres, isolated control workload, gate passed (`testbed-lab-design.md` §13) | DONE | B1, A7 |
| B3 | Control plane outside the lab, its own Postgres; the Connector inside the lab dials out (`testbed-control-plane-design.md` §10) | DONE (all seven gate items pass, including a lab recreation with the history intact) | A7, B2 |
| B4 | Scenarios, repeats, dev and held-out split, frozen manifest, runner (`testbed-scenarios-design.md`) | ACTIVE: slices 1 and 2 run (3 repeats each); slice 3 (`config-or-rollout`) run (3 repeats), invalid under the old ordering rule and valid after the owner-approved ordering amendment (`testbed-ground-truth-contract.md` §14, sub-round order is not observable); then `scheduled-recurring`, `competing-causes`, `negative-control` (needs the decoy amendment). No `HOLDOUT` tier yet | B1, B2, B3 |
| B5 | Frozen engine baseline: version and acceptance criteria fixed before any run | ACTIVE: done per slice (each manifest frozen with engine 2.1.0 and the acceptance thresholds before its runs); the whole-suite freeze follows the last family | B4 |
| B6 | Multi-cutoff recordings per run, to test timing stability against a known world | NEXT | B4 |
| B7 | Product-mode long run: the lab with a randomized fault injector and the control plane as a customer would run it | DONE (10 hours, 300 diagnoses; findings fixed in A12, C9, C13 and the lab's Tempo limit of 2 GiB; a 2-hour run after the fixes: 25 incidents, 50 diagnoses, 0 Loki or Tempo failures, transport wait median 1.98 s, max 6.78 s) | B3 |

### Testbed results so far (2026-09-30, all `DEV`, engine 2.1.0)

| Slice | Fault | Valid runs | Cause and instance named | Execution witness | False strong authority / false `RESOLVED` |
|---|---|---|---|---|---|
| 1 | network delay on `payment-service` | 3/3 | 3/3 | 0/3 (a delay leaves no pod-level failure) | 0 / 0 |
| 2 | CPU stress on `order-service` | 3/3 | 3/3 | 1/3 (first real witness, checked against the truth) | 0 / 0 |
| 3 | `payment-service` environment change (rollout) | 3/3 (after §14; 0/3 before) | 3/3, instance 2/3 | 0 (no rollout rule yet, C5; expected) | 0 / 0 |

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
| C3 | General fault-to-downstream-effect explanation (needs path evidence). **Measured gap (2026-10-03, run `indep1`): no asynchronous messaging edge.** In 2 incidents (4 diagnoses: `KafkaConsumerLag`, `OrderWorkerLagHigh`) the true cause, a CPU stress on `order-service` (`StressChaos` ic-6), was never a candidate: `UNLINKED`, admitted only as context (`NO_POSITIVE_INCIDENT_LINK`). Prometheus shows the mechanism: during the stress `order-service` produced 65 instead of about 335 messages per 30 s and `order-worker` consumed in step; on recovery production resumed slightly ahead of consumption (76 against 60), which is what the lag alert measures. The topology has `calls` (from configured URLs), `runtime_propagates` (from traces), ownership, selection, configuration and disruption, but no producer → topic → consumer relation, so a fault on a producer cannot reach a consumer-side symptom, while older candidates with a path (`payment-service` as a dependency of `order-worker`) fill the supported tier. The §11 filter correctly left those diagnoses `COMPETING` (no candidate tied to the onset). Needed: a messaging relation from observations (messaging spans, or topic references in configuration), measured on the testbed before use | NEXT |
| C4 | Frontier closure by evidence | NEXT |
| C5 | New strong rules (rollout, config consumption, autoscaling) on the same witness and effect skeleton | NEXT |
| C6 | `RESOLVED`: every declared symptom covered by witnesses | NEXT |
| C7 | Capture history for live and replay sources (timing stability is `UNASSESSED` there) | NEXT |
| C9 | Evidence timing of a resolved incident: its window freezes at resolution, so an event that happened before the resolution but reached the control plane seconds later (the connector's event path lags about 15 s) is never seen, e.g. an experiment's `Recovered`. Decide the membership rule (event time versus arrival time, with or without a bounded arrival grace) in the causal semantics contract first | ACTIVE: design approved (`late-evidence-design.md`); A (membership by Connector observation) and B (coverage record, transport proof, persisted gaps, continuity across control-plane restarts via `stream_follow_segments`) DONE and checked live (§9); the transport wait was measured, not assumed: the change-stream heartbeat interval dominates it; next, the engine readings of §5 one by one, starting with `observed-fault-execution` |
| C10 | **`STALE_CAUSAL_EVIDENCE` (P1, product defect).** An incident with no candidate that has evidence in its window still shows a leading actor chosen from evidence hours old. Measured on the 22 testbed databases: 23 of 122 incidents (19%) led with an actor whose every finding was more than an hour old, all of them `INSUFFICIENT_EVIDENCE` / `UNESTABLISHED` (e.g. `KafkaConsumerLag` at a load step led by an `order-worker` pod whose container failed 4.1 h earlier; the failure comes from the pod's current status, so no journal lookback bounds it). The epistemic state is right; the defect is that "outside the incident window" is only a ranking penalty (`ranking.py`), so the least-bad candidate still becomes `root_cause`, and the console shows it as "Possible causal actor", in the incident list and in the diagnosis notification. **Presentation fix DONE (owner-approved):** the engine marks `leading_actor_established` (outside the epistemic digest; `root_cause` and the ranking are unchanged) when no claim is established and every timed finding of the leader lies outside the window; the list, workspace, change highlighting and reports show "Not established", and the notification names no actor. Replayed over all 108 testbed incidents with a stored run: 23 withheld (all `INSUFFICIENT_EVIDENCE`, exactly the characterized set), 85 unchanged; 105 digests equal, and the 3 that differ are one phase-0 run recorded before `5874ca3`, equal when replayed with the engine before that fix. **Engine option measured in shadow, not adopted:** on the 35 ITBench scenarios it changed nothing (same prediction, confidence and correctness everywhere); on the 108 testbed incidents 85 kept their leader, 18 lost it, and **5 got another leader** (a `payment-service` pod with an in-window probe event, for a Kafka-lag incident caused by a load step, so one wrong actor replaced by another); none of the 20 supported or strong cases changed. The presentation fix already covers the 23 cases without promoting a new actor | DONE (presentation); engine option not adopted |
| C11 | **ITBench accuracy moved since the published run.** Found while measuring C10: the current engine answers 18 of the 31 scoreable scenarios correctly, against 26 at the v1.1.2 freeze (dev 5/9 today, 9/9 on 2026-09-26). Of the 13 predictions that changed, 11 moved from a spawned experiment to its parent `Schedule` instance (the owner-approved Schedule carrier, 2026-09-30); ITBench's labels name the experiment, and 3 of those 11 still score because the label pattern also matches the Schedule. The other two: Scenario-38 (unscoreable), and Scenario-34, which moved from `valkey-cart` to `cart` at `80d3356` (M21 A4 P3, onset from alert-channel coverage; found by bisect, then a decision-trace diff across that commit). Stage by stage: (1) the onset moved from 17:26:52 to 17:43:35; (2) with it, a CPU-throttling rise on `cart-755465879b` (17:38:46, 289 s before the new onset) became a pressure finding and a new candidate; (3) it scored 8.0, exactly as `valkey-cart`; (4) the tie was broken by the canonical name (`ranking.py`, `(-score, entity.canonical)`), so `cart` leads. Neither candidate was ever supported, before or after: the earlier correct answer was also a ranking outcome. The defect this exposes is not the onset rule but a **leader chosen alphabetically between equally scored, unsupported candidates**, contrary to the rule that canonical order is never causal evidence (`docs/architecture.md`). Decide how a Schedule answer for a spawned-experiment label is graded (without tuning to labels), explain Scenario-34, and stop quoting 26/31 as the current engine's accuracy | DECISION |
| C12 | **Tied leaders are decided by name order.** Measured with the engine's own candidate pool (`_root_cause_selectable_hypotheses`, not eliminated), top score shared by more than one candidate: ITBench 15 of 35, testbed 34 of 108. Among them the name order decided the reported `root_cause` in 14 (ITBench) and 20 (testbed); only 1 and 1 were `UNESTABLISHED` (Scenario-34 and one testbed incident); the rest are `POSSIBLE_INITIATING_CAUSE`, mostly with two or more supported claims tied (12 and 19), often with an unsupported candidate carrying the same findings (a pod sharing an experiment's findings); one tie in the testbed involved a strong claim. The score does not separate epistemic levels: tied candidates differed in support or state in 9 of 15 and 28 of 34. Canonical order also favours the `chaos-mesh` namespace over workload namespaces. **Presentation DONE (owner-approved, `994589d`):** the operator's leader is projected by epistemic tier (strong > supported > unestablished) within the engine's candidate pool: one actor in the top tier is shown alone, several supported or strong ones as competing causes, an unestablished tier as not established when its top score is shared (`TIED_LEADERS`) or its leader has no evidence in the window (C10); `root_cause`, ranking and digest unchanged. Replayed over the clean testbed incidents (88): 45 single supported, 1 single strong, 8 single unestablished, 4 competing, 24 not established (23 window, 1 tied), 6 without candidates; 85 digests equal, 3 differ (the pre-`5874ca3` run). The one clean case where the engine's `root_cause` is an unsupported pod ahead of a supported experiment now shows the experiment. **Contamination:** 20 incidents in 4 phase-0 databases carry the manual `diag-stress` experiment run while calibrating the CPU stress (unscored runs; excluded here); 8 of the 9 earlier unsupported-wins cases were among them. **Engine selection (rank within the highest tier, name order only as the last tie-break):** contract approved and implemented (`m21-causal-semantics-contract.md`, `leader_by_tier`), after a shadow measurement: ITBench no change, testbed one change (to the true cause), nothing lost; the reported `root_cause` and the operator's projection now use the same tiers. Harness isolation from faults a run did not create added (`d10d8e4`) | DONE |
| C13 | Diagnosis only on an incident change (`diagnosis-trigger-design.md`): created `INITIAL`, continued `ALERT_REFIRED`, resolved `RESOLVED`; nothing re-diagnosed after a restart; bulk log capture serialized | DONE (owner-approved; verified in the 2-hour run: 25 `INITIAL`, 25 `RESOLVED`, 0 after a restart) |
| C14 | Chaos experiments journaled as objects (`chaos-objects-design.md`), the prerequisite of C2's `spec.action` | DONE (verified live; acceptance passed: slices 1 to 3 re-run, no false strong authority or false `RESOLVED`, named causes unchanged, one new execution witness explained, `chaos-objects-design.md` §6; API cost not measured) |
| C15 | **Temporal relevance of supported leadership** (`m21-causal-semantics-contract.md` §11): a competing supported candidate whose observed effect ended more than `W` before the onset yields to a linked candidate tied to the onset by an observation; never produces `NOT_ESTABLISHED`, `W` not fixed. 252 of 300 long-run diagnoses were `COMPETING`, two thirds of their candidates stale. Shadow (`packages/evals/temporal_relevance.py`): 10-hour run 186 of 210 fault-time `COMPETING` to `SINGLE`, all the true cause; 2-hour run 30 of 30 at `W` 5 and 15 min; true cause demoted 0, `NOT_ESTABLISHED` +0; slices and ITBench unchanged | DONE (presentation): independent check passed (pre-registered, §11.1 and §11.2: run `indep1`, 11 faults with overlaps, no hard violation at any `W`; 63 of 78 `COMPETING` to `SINGLE` at `W` = 5, both causes kept in every overlap); `W` = 5 min by the pre-registered rule. **Adopted for presentation** (owner-approved 2026-10-03, §11.3): re-diagnosis of the 41 `indep1` incidents matches the shadow 41 of 41, `root_cause` and digest unchanged; moving it into engine leader selection is a separate decision |
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
| F2 | Name of the checkpoint that authorizes a push | DECISION (in practice: branch, PR, CI, merge on the owner's word, pinned to the CI-tested commit) |
| F3 | Old in-cluster `control-plane` deployment | DONE (moot: the recreated lab never deploys it; the control plane runs outside, B3) | |
| F4 | `.local` upkeep: compress `baseline-rivals*.json`, prune superseded run directories after checking references. Lab images: every `kind load` of a rebuilt image leaves the previous one on the node as an untagged `import-<date>` reference that kubelet only collects above 85% disk use (2026-10-02: 8 old Connector images, 2.3 GB of the node's 10.1 GB volume, removed by hand with `ctr -n k8s.io images rm` on the orphan references only); add that cleanup after each load to the lab targets. Host disk had 17 GiB free before an 8.5 GB build-cache prune, 24 GiB after | NEXT |
| F5 | Rollout intermediate versions are superseded in the change mirror (about 48 per rollout; the catch-up count is separated from real losses); belongs with C5's rollout rules | NEXT |
| F6 | The kind node restarted once by itself during a run (restart policy on-failure); cause unknown | NEXT (watch) |

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
