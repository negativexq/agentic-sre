# Roadmap (2026-10-05)

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
| A6 | Live traces in the base diagnosis (`live-trace-design.md`): a fault and a baseline window per instrumented service, at most two Tempo reads at once; live `query_traffic` still not read | DONE for traces (on by default since 2026-10-04, §11); traffic PARKED | B |
| A7 | Physical separation: gRPC over mTLS, connector dials out, static certificates (contract §12, §13) | DONE (all suites pass over gRPC; the stream-mode default flip stays a separate decision; `uv lock` left to the owner) | A4 |
| A10 | Watch-driven change stream (contract §15, `connector-watch-design.md`): per-scope LIST then WATCH, `Gap(RESOURCE_VERSION_EXPIRED)` and a snapshot on an expired version, observed and inferred deletions, the control plane journaling as changes arrive, wire `connector.v2`. Change-to-journal median 21.5 s → 1.2 s (p90 27.1 s → 1.9 s); API requests about 6.5 times fewer; API outage, connector restart and an expired version recovered in the lab (§15.1, §15.2) | DONE (three-hour soak, §15.5: 0 failures, 35 scope gaps on quiet Event scopes, 0 global snapshots, end-to-end p50 1.19 s / p90 2.01 s) | A7 |
| A11 | Synthetic bookmarks on quiet Event scopes (contract §15.6): a `limit=1` LIST every 120 s while the scope's watch is open, adopted only by the same watch at a normal end | DONE (owner-approved; soak: 0 Event version expiries in 137 min, against 35 in 180 before) | A10 |
| A12 | Long-run findings on the connector path: a replaced alert occurrence ends at the new `startsAt` (`incident-episode-contract.md` §9); chaos kinds listed in every journaled namespace, a 403 or 404 outside the chaos namespaces skipped with one warning instead of an endless relist; lab RBAC reads `chaos-mesh.org` (`chaos-objects-design.md`) | DONE (10-hour product run found them; a 2-hour run after the fixes: 0 stuck incidents, 0 Event gaps, 4/4 experiments journaled) | A10 |
| A8 | `connectorctl preflight` with real probing, enrollment token, certificate issue and rotation, Helm (`connector-install-design.md`, owner-approved 2026-10-09) | ACTIVE: A8.1 preflight DONE (12/12 checks ok against the lab as the Connector's ServiceAccount; read-only verified both ways); A8.2 registry and enrollment DONE (one-time token carrying the CA, a separate enrollment port, owner amendment §7; verified over gRPC on loopback); next A8.3 rotation and revocation | A7 |
| A9 | Remote and multi-tenant operation, connector version compatibility | PARKED | A8 |

## 3. B. Testbed (an instrumented causal lab, not a benchmark)

| # | Work | Status | Depends on |
|---|---|---|---|
| B0 | Hygiene: `orders.created` is declared and re-created by a sidecar and Kafka keeps its data across container restarts (verified live); 88 unused Docker volumes removed and one kept | DONE | |
| B1 | `Timeline` ground truth (`testbed-ground-truth-contract.md`): manifest, journal, oracle, chain and scoring implemented and tested; scenarios and runner wiring come with B4 | DONE | |
| B2 | Recreate the lab cluster: Chaos Mesh 2.8.4 with containerd values, self-migrating Postgres, isolated control workload, gate passed (`testbed-lab-design.md` §13) | DONE | B1, A7 |
| B3 | Control plane outside the lab, its own Postgres; the Connector inside the lab dials out (`testbed-control-plane-design.md` §10) | DONE (all seven gate items pass, including a lab recreation with the history intact) | A7, B2 |
| B4 | Scenarios, repeats, dev and held-out split, frozen manifest, runner (`testbed-scenarios-design.md`) | ACTIVE: slices 1 and 2 run (3 repeats each); slice 3 (`config-or-rollout`) run (3 repeats), invalid under the old ordering rule and valid after the owner-approved ordering amendment (`testbed-ground-truth-contract.md` §14, sub-round order is not observable); slice 4 (`scheduled-recurring`) failed validity (1/3: a 20 s spawn never raised a latency alert) and was amended (60 s every 90 s, latency alerts only, since the lag alerts fire without a fault); slice 4b 3/3 valid, acceptance holds; slice 5 (`competing-causes`, contract §15: symptom groups scored per incident) 3/3 valid, group recall 1.0, cross-attribution 0, acceptance holds; slice 6 (`negative-control`, contract §16: a real change plus a decoy on the isolated workload) 3/3 valid, decoy never named, acceptance holds; **all six families measured in `DEV`**. `HOLDOUT` tier: variant B of every family (see E2); three variants amended on validity alone, each after a blind phase 0 (`scheduled-b` 90 s every 120 s, §15; loss 80–90%, §17; the competing pod-kill near the loss's end and a longer collection, §18) | B1, B2, B3 |
| B5 | Frozen engine baseline: version and acceptance criteria fixed before any run | DONE: manifests carry `engine_commit` and the runner refuses an engine that differs from it (design §12.2.3); the first `HOLDOUT` ran frozen at `96a10c3c` | B4 |
| B6 | Multi-cutoff recordings per run, to test timing stability against a known world | NEXT | B4 |
| B7 | Product-mode long run: the lab with a randomized fault injector and the control plane as a customer would run it | DONE (10 hours, 300 diagnoses; findings fixed in A12, C9, C13 and the lab's Tempo limit of 2 GiB; a 2-hour run after the fixes: 25 incidents, 50 diagnoses, 0 Loki or Tempo failures, transport wait median 1.98 s, max 6.78 s) | B3 |

### Testbed results so far (2026-09-30, all `DEV`, engine 2.1.0)

| Slice | Fault | Valid runs | Cause and instance named | Execution witness | False strong authority / false `RESOLVED` |
|---|---|---|---|---|---|
| 1 | network delay on `payment-service` | 3/3 | 3/3 | 0/3 (a delay leaves no pod-level failure) | 0 / 0 |
| 2 | CPU stress on `order-service` | 3/3 | 3/3 | 1/3 (first real witness, checked against the truth) | 0 / 0 |
| 3 | `payment-service` environment change (rollout) | 3/3 (after §14; 0/3 before) | 3/3, instance 2/3 | 0 (no rollout rule yet, C5; expected) | 0 / 0 |
| 4b | `Schedule` spawning a 60 s delay on `payment-service` every 90 s | 3/3 (slice 4, 20 s every minute: 1/3) | 3/3, Schedule instance 3/3 | 0/3 (a delay leaves no pod failure, as slice 1) | 0 / 0 |
| 5 | delay on `payment-service` + pod-kill of `order-worker` (competing) | 3/3 | 2/2 causes and instances per run, each in its own group (cross-attribution 0) | 0/3 | 0 / 0 |
| 6 | config change on `payment-service` + decoy delay on `lab-control/isolated-echo` (negative control) | 3/3 | 3/3, instance 3/3; decoy named 0 | 0/3 (no rollout rule yet, C5) | 0 / 0 |

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
| C1 | Service-level effect relation (`m21-causal-semantics-contract.md` §12): a client span of the symptom service paired with the server span at the exact target pod, its calls in the execution interval against a baseline before the onset; `N` = 3, `F` = 3, `D` = 0.2 s chosen on `DEV` (§12.6), wired as a fault-execution witness (§12.7) | DONE: confirmed on the fifth and sixth `HOLDOUT` (§12.8; 18/18 valid each, every trace read complete, every witness on the chain). ITBench-Lite cannot exercise it (no chaos execution recorded around any onset) |
| C2 | Observe the fault action (`spec.action`) and the first target-local effect time; bind resource-pressure findings (CPU throttling, memory) to the pod instance that held the name at that time, so a saturating fault's effect is an effect of the exact instance (testbed slice 2: the witness formed in 1 of 3 runs, only where a probe also failed) | NEXT |
| C3 | General fault-to-downstream-effect explanation (needs path evidence). **Measured gap (2026-10-03, run `indep1`): no asynchronous messaging edge.** In 2 incidents (4 diagnoses: `KafkaConsumerLag`, `OrderWorkerLagHigh`) the true cause, a CPU stress on `order-service` (`StressChaos` ic-6), was never a candidate: `UNLINKED`, admitted only as context (`NO_POSITIVE_INCIDENT_LINK`). Prometheus shows the mechanism: during the stress `order-service` produced 65 instead of about 335 messages per 30 s and `order-worker` consumed in step; on recovery production resumed slightly ahead of consumption (76 against 60), which is what the lag alert measures. The topology has `calls` (from configured URLs), `runtime_propagates` (from traces), ownership, selection, configuration and disruption, but no producer → topic → consumer relation, so a fault on a producer cannot reach a consumer-side symptom, while older candidates with a path (`payment-service` as a dependency of `order-worker`) fill the supported tier. The §11 filter correctly left those diagnoses `COMPETING` (no candidate tied to the onset). Needed: a messaging relation from observations (messaging spans, or topic references in configuration), measured on the testbed before use | C3a DONE: observed message delivery (`m21` §15) gives a producer's fault a structural path to consumer-side symptoms; targeted tenth `HOLDOUT` confirmed. Async exclusion (Topic C) not started |
| C4 | Frontier closure by evidence | NEXT |
| C5 | New strong rules on the same witness and effect skeleton. **C5a** (`m21` §13): a rollout's new pods, UID-bound, through the service-level effect; confirmed on the seventh `HOLDOUT` (13 witnesses, none off the chain). **C5b** (§14): a rollout whose new pods never serve while the old revision is removed, read from unserved calls at the callers; the eighth `HOLDOUT` found the implementation accepted only `SPEC_CHANGE` origins (an image change never looked at), fixed to every template-changing origin, confirmed on the ninth (12 witnesses in 4 of 6 held-out failed rollouts, none off the chain) | DONE for rollouts; config consumption without a rollout and autoscaling not started |
| C6 | `RESOLVED`: every declared symptom covered by witnesses | NEXT |
| C7 | Capture history for live and replay sources (timing stability is `UNASSESSED` there) | NEXT |
| C9 | Evidence timing of a resolved incident: its window freezes at resolution, so an event that happened before the resolution but reached the control plane seconds later (the connector's event path lags about 15 s) is never seen, e.g. an experiment's `Recovered`. Decide the membership rule (event time versus arrival time, with or without a bounded arrival grace) in the causal semantics contract first | ACTIVE: design approved (`late-evidence-design.md`); A (membership by Connector observation) and B (coverage record, transport proof, persisted gaps, continuity across control-plane restarts via `stream_follow_segments`) DONE and checked live (§9); the transport wait was measured, not assumed: the change-stream heartbeat interval dominates it; next, the engine readings of §5 one by one, starting with `observed-fault-execution` |
| C10 | **`STALE_CAUSAL_EVIDENCE` (P1, product defect).** An incident with no candidate that has evidence in its window still shows a leading actor chosen from evidence hours old. Measured on the 22 testbed databases: 23 of 122 incidents (19%) led with an actor whose every finding was more than an hour old, all of them `INSUFFICIENT_EVIDENCE` / `UNESTABLISHED` (e.g. `KafkaConsumerLag` at a load step led by an `order-worker` pod whose container failed 4.1 h earlier; the failure comes from the pod's current status, so no journal lookback bounds it). The epistemic state is right; the defect is that "outside the incident window" is only a ranking penalty (`ranking.py`), so the least-bad candidate still becomes `root_cause`, and the console shows it as "Possible causal actor", in the incident list and in the diagnosis notification. **Presentation fix DONE (owner-approved):** the engine marks `leading_actor_established` (outside the epistemic digest; `root_cause` and the ranking are unchanged) when no claim is established and every timed finding of the leader lies outside the window; the list, workspace, change highlighting and reports show "Not established", and the notification names no actor. Replayed over all 108 testbed incidents with a stored run: 23 withheld (all `INSUFFICIENT_EVIDENCE`, exactly the characterized set), 85 unchanged; 105 digests equal, and the 3 that differ are one phase-0 run recorded before `5874ca3`, equal when replayed with the engine before that fix. **Engine option measured in shadow, not adopted:** on the 35 ITBench scenarios it changed nothing (same prediction, confidence and correctness everywhere); on the 108 testbed incidents 85 kept their leader, 18 lost it, and **5 got another leader** (a `payment-service` pod with an in-window probe event, for a Kafka-lag incident caused by a load step, so one wrong actor replaced by another); none of the 20 supported or strong cases changed. The presentation fix already covers the 23 cases without promoting a new actor. **Near the onset (`m21` §18, owner-approved 2026-10-08):** the window reaches two hours back, so a leader resting on a finding 13 minutes old still showed alone (twelfth `HOLDOUT`, an incident opened before its run's fault, led by the previous fault's pod); an unestablished leader now also needs a finding at or after the onset less 5 minutes (§11's `W`, reused). Offline over 322 databases: 2,412 of 2,412 digests and roots equal, no scored incident changed, 7 `SINGLE` leaders withheld (findings 8.8 to 38.7 min old), engine 2.2.2 | DONE (presentation, C10 and §18); engine option not adopted |
| C11 | **ITBench accuracy moved since the published run.** Found while measuring C10: the current engine answers 18 of the 31 scoreable scenarios correctly, against 26 at the v1.1.2 freeze (dev 5/9 today, 9/9 on 2026-09-26). Of the 13 predictions that changed, 11 moved from a spawned experiment to its parent `Schedule` instance (the owner-approved Schedule carrier, 2026-09-30); ITBench's labels name the experiment, and 3 of those 11 still score because the label pattern also matches the Schedule. The other two: Scenario-38 (unscoreable), and Scenario-34, which moved from `valkey-cart` to `cart` at `80d3356` (M21 A4 P3, onset from alert-channel coverage; found by bisect, then a decision-trace diff across that commit). Stage by stage: (1) the onset moved from 17:26:52 to 17:43:35; (2) with it, a CPU-throttling rise on `cart-755465879b` (17:38:46, 289 s before the new onset) became a pressure finding and a new candidate; (3) it scored 8.0, exactly as `valkey-cart`; (4) the tie was broken by the canonical name (`ranking.py`, `(-score, entity.canonical)`), so `cart` leads. Neither candidate was ever supported, before or after: the earlier correct answer was also a ranking outcome. The defect this exposes is not the onset rule but a **leader chosen alphabetically between equally scored, unsupported candidates**, contrary to the rule that canonical order is never causal evidence (`docs/architecture.md`). Decide how a Schedule answer for a spawned-experiment label is graded (without tuning to labels), explain Scenario-34, and stop quoting 26/31 as the current engine's accuracy **Measured (2026-10-08, `c11-itbench-equivalence.md`):** the answer ITBench receives stays `root_cause`; evidence-backed tracks are sealed with each prediction and graded apart. Engine 2.2.2: exact **18/31**; causal equivalence (the root `Schedule` instance's own spawned experiment ran across the onset) **20/31**; structural controller record 26/31; executing instance 18/31. Of the 13 exact misses: 2 proven equivalences (81, 83), 6 possible only (17, 21, 35 ran an hour earlier; 22, 80, 91 began after the onset, C18), 4 the label cannot score (1, 20, 33, 102), 1 wrong causal actor (34). README keeps 26/31 as historical | DONE |
| C12 | **Tied leaders are decided by name order.** Measured with the engine's own candidate pool (`_root_cause_selectable_hypotheses`, not eliminated), top score shared by more than one candidate: ITBench 15 of 35, testbed 34 of 108. Among them the name order decided the reported `root_cause` in 14 (ITBench) and 20 (testbed); only 1 and 1 were `UNESTABLISHED` (Scenario-34 and one testbed incident); the rest are `POSSIBLE_INITIATING_CAUSE`, mostly with two or more supported claims tied (12 and 19), often with an unsupported candidate carrying the same findings (a pod sharing an experiment's findings); one tie in the testbed involved a strong claim. The score does not separate epistemic levels: tied candidates differed in support or state in 9 of 15 and 28 of 34. Canonical order also favours the `chaos-mesh` namespace over workload namespaces. **Presentation DONE (owner-approved, `994589d`):** the operator's leader is projected by epistemic tier (strong > supported > unestablished) within the engine's candidate pool: one actor in the top tier is shown alone, several supported or strong ones as competing causes, an unestablished tier as not established when its top score is shared (`TIED_LEADERS`) or its leader has no evidence in the window (C10); `root_cause`, ranking and digest unchanged. Replayed over the clean testbed incidents (88): 45 single supported, 1 single strong, 8 single unestablished, 4 competing, 24 not established (23 window, 1 tied), 6 without candidates; 85 digests equal, 3 differ (the pre-`5874ca3` run). The one clean case where the engine's `root_cause` is an unsupported pod ahead of a supported experiment now shows the experiment. **Contamination:** 20 incidents in 4 phase-0 databases carry the manual `diag-stress` experiment run while calibrating the CPU stress (unscored runs; excluded here); 8 of the 9 earlier unsupported-wins cases were among them. **Engine selection (rank within the highest tier, name order only as the last tie-break):** contract approved and implemented (`m21-causal-semantics-contract.md`, `leader_by_tier`), after a shadow measurement: ITBench no change, testbed one change (to the true cause), nothing lost; the reported `root_cause` and the operator's projection now use the same tiers. Harness isolation from faults a run did not create added (`d10d8e4`) **Residue closed (m21 §20, owner-approved 2026-10-08):** a leader not established still leaked through `root_cause` into the summary, the remediation (ITBench Scenario-34: "raise the cpu limit of deployment/cart", `cart` chosen over `valkey-cart` by name) and the event, timeline, HTML report and v1 API; those now read the shown leader and propose nothing, `root_cause` stays the ranking's choice for replay; engine 2.3.1; replay: 2,412 digests and roots equal, the 77 `NOT_ESTABLISHED` diagnoses lose their remediation, the 43 tied ones their single-actor summary. What an ITBench answer is in that case stays an owner decision | DONE |
| C13 | Diagnosis only on an incident change (`diagnosis-trigger-design.md`): created `INITIAL`, continued `ALERT_REFIRED`, resolved `RESOLVED`; nothing re-diagnosed after a restart; bulk log capture serialized | DONE (owner-approved; verified in the 2-hour run: 25 `INITIAL`, 25 `RESOLVED`, 0 after a restart) |
| C14 | Chaos experiments journaled as objects (`chaos-objects-design.md`), the prerequisite of C2's `spec.action` | DONE (verified live; acceptance passed: slices 1 to 3 re-run, no false strong authority or false `RESOLVED`, named causes unchanged, one new execution witness explained, `chaos-objects-design.md` §6; API cost not measured) |
| C15 | **Temporal relevance of supported leadership** (`m21-causal-semantics-contract.md` §11): a competing supported candidate whose observed effect ended more than `W` before the onset yields to a linked candidate tied to the onset by an observation; never produces `NOT_ESTABLISHED`, `W` not fixed. 252 of 300 long-run diagnoses were `COMPETING`, two thirds of their candidates stale. Shadow (`packages/evals/temporal_relevance.py`): 10-hour run 186 of 210 fault-time `COMPETING` to `SINGLE`, all the true cause; 2-hour run 30 of 30 at `W` 5 and 15 min; true cause demoted 0, `NOT_ESTABLISHED` +0; slices and ITBench unchanged | DONE (presentation): independent check passed (pre-registered, §11.1 and §11.2: run `indep1`, 11 faults with overlaps, no hard violation at any `W`; 63 of 78 `COMPETING` to `SINGLE` at `W` = 5, both causes kept in every overlap); `W` = 5 min by the pre-registered rule. **Adopted for presentation** (owner-approved 2026-10-03, §11.3): re-diagnosis of the 41 `indep1` incidents matches the shadow 41 of 41, `root_cause` and digest unchanged; moving it into engine leader selection is a separate decision |
| C16 | **Execution witnesses that the held-out set lost.** The effect relations' baseline was a fixed window `[onset − 10 min, onset − 5 min]`, empty before the run's load or holding the previous run's traffic; it is now the five minutes before the first execution (`m21` §16). Offline replay of every stored run: 10 witnesses gained and 2 lost, all on the chain, none off; targeted eleventh `HOLDOUT`: 15/15 valid, 19 witnesses, none off the chain. `scheduled-b` still has none in that `HOLDOUT` | DONE (baseline); `scheduled-b` stays open |
| C17 | Replay against live: 71 of 271 stored diagnoses did not reproduce their live epistemic digest when replayed with a later engine. **Measured (2026-10-05):** every diagnosis of the eleven testbed `HOLDOUT`s replayed with the engine commit its manifest froze (eight commits, each in its own worktree, `trajectory` mode): **2,876 of 2,876 digests equal**. Replay is deterministic for a given engine; the earlier gap is engine evolution, not a defect. **The product gap it shows:** a diagnosis records `engine_version` 2.1.0, which has not changed while the engine has (F1), so a stored diagnosis cannot tell which engine to replay it with; the owner chose to version the engine instead (F1: 2.2.0 and a versioning rule) | DONE |
| C18 | **An execution that began after the onset is presented as the initiator.** `annotate_temporal_roles` (`ranking.py`) marks a change or fault finding `INITIATING` up to `verification_onset_grace` (15 min) *after* the causal onset, and `m21.support.change-onset-path` then supports it; the grace suits a change journaled late, not a controller event whose own timestamp is exact. Found during C11 (2026-10-08): on ITBench, Scenario-22, 80 and 91 name a `Schedule` instance whose first application came 12 to 15 minutes after the onset. Reproduced without ITBench: 33 stored testbed diagnoses lead with an actor whose every initiating finding came after the onset (`DEV` `slice4b`: a later spawn of the run's own `Schedule`, 49 to 59 s after; twelfth `HOLDOUT` `competing-b`: `pod-kill-287`, 3 to 68 s after), all `SUPPORTED`. Related, not the same: the causal onset (m21 §10.2) is the first diagnostic episode that began inside alert coverage, while alerts already firing when coverage began are excluded; on 6 of the 8 ITBench `Schedule` scenarios diagnostic alerts began 3 to 14 min before coverage, so the actual symptom onset is earlier and unknown, and it is represented as one instant rather than as uncertainty. Principles agreed (owner, 2026-10-08): first observed alert is not the actual symptom onset; an unknown onset is uncertainty; supported evidence is never invalidated for its age (§11); an execution that began after the latest admissible onset never carries the initiator role. Next: reproduce on `DEV` (`scheduled-b` records, the negative variants), then a contract text; ITBench is not used to design it **Implemented (m21 §19, owner-approved 2026-10-08):** a controller-recorded execution more than `δ` = 1 s (measured on `DEV`, §19.7) after the causal onset is `AFTER_ONSET`, never `INITIATING`; the comparison is with the causal onset, not the earliest alert (a chronic alert would otherwise strip a real initiator); the onset's uncertainty is recorded (`began_before_coverage`, `earliest_alert_start`); engine 2.3.0. Offline over 2,412 testbed incidents: no root cause moves off the chain; scheduled runs now lead with the spawn applied before the onset; two scored pod-kills are no longer shown, each applied after every incident it had been credited for (§19.8) Thirteenth `HOLDOUT` (targeted, 6/6 valid): every criterion held, no leader from an execution after the onset, every root on the chain; the one cause not named raised no incident of its own and is listed `AFTER_ONSET` in the loss's incidents (`testbed-scenarios-design.md` §27.1) | DONE |
| C19 | **A `Schedule` claim's members mix incarnations.** The topology's `spawns` edge is derived from names (`topology.py`: an experiment whose name is the `Schedule`'s plus a suffix of at most 7 characters, in the same namespace), not from the controller's `Spawned` record of a UID. So when two incarnations of one `Schedule` name are in the evidence, each `Schedule` claim gathers both incarnations' experiments as members: their findings enter the claim's score and the evidence listed with it. Support and authority are not affected: D1 reads only the actor's own findings of the claim's exact instance, and the execution witness checks the schedule UID. First read (C18's ITBench regression) as a claim supported by another incarnation's evidence; that was C18's own defect, fixed by it (m21 §19.9 corrected). Impact: ranking within one tier and presentation only. Fix, if taken: derive `spawns` from the `Spawned` records (UID-bound) where they exist | NEXT (low) |
| C8 | Cost reduction for large onset sets (one representative per breakpoint) | PARKED |

## 5. D. Product surface

| # | Work | Status |
|---|---|---|
| D1 | UI notification: toast and badge on a new incident, a second one when the diagnosis lands; driven only by persisted state | DONE (verified in the browser against real alerts; `docs/ui/product-contract.md`, Notifications) | |
| D2 | Browser notification while the tab is closed | PARKED |
| D3 | Console shows timing stability, withheld authority, `instance_resolution` and status drivers; and the executing instance beside the root cause (`m21` §17) | DONE (owner-approved design, `docs/ui/product-contract.md` D3): the workspace and the report (v2.4) show what ran on which pod and when, and the leader's instance; timing only when assessed (live sources stay unassessed until C7), its withheld authority and drivers in the investigation tab. Checked in the browser on a copy of a twelfth-`HOLDOUT` database re-diagnosed with 2.2.2 |
| D4 | Connect Cluster flow (token, install command, health, partial-coverage states) | NEXT (after A8) |

## 6. E. Benchmark suite (on top of the testbed)

| # | Work | Status |
|---|---|---|
| E1 | Metamorphic tests (identity, onset shift, renaming) and multi-cutoff tests | NEXT |
| E2 | Held-out set: own live scenarios with world-level ground truth (the same work as B4) | DONE for the first `HOLDOUT` (`testbed-scenarios-design.md` §12): variant B of every family, engine frozen by commit, blind phase 0; 18/18 valid, no false strong authority or false `RESOLVED`, decoy never named, cause recall 1.0 except one competing run whose second cause raised no symptom in time (0.83). After C1 (engine frozen at `58eadf38`, new seeds each time): second and third could not decide C1 (failed trace reads; the third also 16/18 valid), fourth stopped by the owner after two invalid runs, fifth 18/18 valid and C1 confirmed but the competing second cause unnamed (world and collection, §17.2), sixth 18/18 valid with both competing causes named and C1 held, seventh (C5a, two variants C added) 24/24 valid and C5a confirmed, eighth 24/24 valid with C5b not exercised (an implementation defect), ninth 24/24 valid and C5b confirmed (`testbed-scenarios-design.md` §13 to §22.1). No false strong authority, false `RESOLVED` or named decoy in any of them. Next `HOLDOUT` only after an engine change |
| E3 | Metrics: execution witness recall, effect-link recall, propagation-link recall, correct causal family, correct exact instance when knowable, false strong authority, false `RESOLVED`, time to resolution, evidence and read cost | NEXT |

## 7. F. Decisions and maintenance

| # | Item | Status |
|---|---|---|
| F1 | Engine version. It stayed 2.1.0 from 2026-09-28 while the engine changed (C1, C5, C3a, C16, leader selection), so a stored diagnosis could not tell which engine to replay it with (C17). **Decision (owner, 2026-10-05):** `RCA_ENGINE_VERSION` 2.2.0; from now on a change that can alter a diagnosis from the same evidence raises the minor version, a change that cannot alter any digest raises the patch, a change of the contract version (`m21.vN`) raises the major. Replay already refuses a run recorded under another version; a test pins the current one | DONE (2.2.0; since then 2.2.1, the executing instance, and 2.2.2, `m21` §18, both patches) |
| F2 | Name of the checkpoint that authorizes a push | DECISION (in practice: branch, PR, CI, merge on the owner's word, pinned to the CI-tested commit) |
| F3 | Old in-cluster `control-plane` deployment | DONE (moot: the recreated lab never deploys it; the control plane runs outside, B3) | |
| F4 | `.local` upkeep: compress `baseline-rivals*.json`, prune superseded run directories after checking references. Lab images: every `kind load` of a rebuilt image leaves the previous one on the node as an untagged `import-<date>` reference that kubelet only collects above 85% disk use (2026-10-02: 8 old Connector images, 2.3 GB of the node's 10.1 GB volume, removed by hand with `ctr -n k8s.io images rm` on the orphan references only); add that cleanup after each load to the lab targets. Host disk had 17 GiB free before an 8.5 GB build-cache prune, 24 GiB after | NEXT |
| F5 | Rollout intermediate versions are superseded in the change mirror (about 48 per rollout; the catch-up count is separated from real losses); belongs with C5's rollout rules | NEXT |
| F7 | **Short alerts reached the product by chance.** An alert shorter than Alertmanager's grouping window became an incident or not depending on the poll's phase and, measured later, on whether its group had been notified before (the webhook then delivers it at the group's `group_interval` flush, already resolved). Fixed by one admission rule on the alert's own active time for both paths (`connector-boundary-contract.md` §16, `SRE_ALERT_MIN_ACTIVE_SECONDS`, 30 s by default); lab: short alerts 0 of 5, lasting 5 of 5 | DONE |
| F6 | The kind node restarted once by itself during a run (restart policy on-failure); cause unknown | NEXT (watch) |
| F8 | Lab Tempo restarts under load: 16 restarts in two days at a 500m CPU limit (liveness timeouts), which failed trace reads in the second and third `HOLDOUT`; raised to 2 CPUs and 5 s probes (`testbed-scenarios-design.md` §15), still two restarts during phase 0 runs since, none during a `HOLDOUT` | NEXT (watch) |
| F9 | `kind-e2e` CI job fails now and then on commits that touch no code (seen on a docs-only commit, green on the next) | NEXT |
| F10 | Harness: a calibration failure before any injection (the `order-service` port-forward serving nothing, only `URLError` in the baseline) stops the suite instead of being set aside like a refused baseline; done by hand once (`.refused-…`, §17.1) | NEXT |
| F11 | Incidents opened for alerts that ended before the installation first observed the alert channel: 74 of 209 `HOLDOUT` runs had one before their injection (165 incidents, none scored). First read as the previous run's alerts (wrong); they are the isolation step's warm-up alerts, delivered late. Fixed in the control plane (`connector-boundary-contract.md` §17, owner-approved 2026-10-08): such an occurrence opens no incident; later coverage gaps unaffected. Offline: 463 unscored incidents not opened, 0 scored affected, runs with a pre-injection incident 74 → 2 | DONE (code and offline); lab check: the fourteenth `HOLDOUT` (full) had **0 of 24** runs with an incident before the injection, against 8 of 24 in the twelfth; §17.5 (10 s alerts admitted) open |
| F12 | Engine: a leader whose fault-execution witness names the exact experiment and pod can still carry `instance_resolution` `UNKNOWN` on its causal family (seen on a twelfth-`HOLDOUT` `StressChaos` leader); the console hides the instance line when an execution line is shown (D3), the inconsistency itself is not explained yet | NEXT |

## 8. Critical path

`A3 -> A4 -> B1 -> B0 -> B2 and A7 -> B3 -> B4 -> B5 -> C1 -> C5 -> C3a -> C16` (done) `-> C17 -> full HOLDOUT -> E3 -> C2, C4, C6`

Rationale: the boundary must be correct before anything is measured through it (A3, A4); the
world must be measurable before a rule is written for it (B); rules follow only where the
testbed can confirm them (C).

## 9. Risks

- **Stream semantics (A4).** Cursor, replay of missed events and `GAP` must behave the same
  in-process and over gRPC; this is the likeliest place for the two transports to diverge.
- **Tuning to examples.** Parameters chosen on one seen scenario fit that scenario (C1 was
  deferred for this reason, then chosen on `DEV` and confirmed on `HOLDOUT`). Dev and held-out
  are separated in B4; a `HOLDOUT` result never selects a rule, a parameter or a variant change
  (variant fixes are decided on validity alone, after a blind phase 0).
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
