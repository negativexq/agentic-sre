# M19 — Evidence-Preserving Diagnosis Revisions and Deterministic Replay

**Status:** FROZEN — correctness foundation merged; remaining synthetic F7 pressure validation superseded
**Date:** 2026-09-27
**Merge:** `3d376853a75686c87537ef1bb0ebc7cc8ba43da5`
**PR:** [#2](https://github.com/negativexq/agentic-sre/pull/2)

## Summary

M19 reduces dependence on transient/current state by binding diagnosis decisions
to persisted evidence, exact entity-instance identity, explicit revisions, and
ordered provider reads. It records why a revision changed and supports
reproducing the persisted epistemic state offline. Later evidence can be
captured and evaluated in a new diagnosis revision without rewriting earlier
revision records.

This is a correctness and auditability foundation, not a claim that the RCA
engine resolves every realistic incident or that the current deployment
provides production-grade durability.

## Major capabilities delivered

### Identity

- Persisted Pod UID and entity-instance identity from observation records into
  findings.
- Exact-UID A1 episode binding, including separate treatment of Pod
  reincarnations.
- Evidence-independent hypothesis keys for matching hypotheses across
  diagnosis revisions.
- Decisive evidence IDs recorded on eliminations.

### Evidence continuity

- Append-only lifecycle observation ledger with sequence and identity
  protections.
- Persisted snapshot cycles containing the captured object bodies.
- Capture → evidence manifest → RCA boundary, with manifest membership built
  from committed evidence.
- Frozen alert/evidence content and append-only safeguards for authoritative
  evidence.

### Provider provenance

- ProviderAdapter mediates Prometheus, Loki, and Tempo provider reads.
- Provider capabilities and ordered per-run provider tape are persisted.
- Provider results, including ERROR results, are committed before they are
  returned to consumers.
- Engine runtime reads are scoped to the current run.

### Replay

- ReplaySource reloads one run's persisted manifest and snapshot.
- ReplayProviderAdapter strictly consumes the ordered tape and reproduces
  recorded provider errors rather than retrying live providers.
- Deterministic selector and recorded investigation-trajectory replay.
- Canonical manifest, provider-tape, and epistemic digests support integrity
  and equivalence checks.

### Revision lifecycle

- Numbered immutable diagnosis revisions with persisted provenance.
- Pure revision diffs matched by stable hypothesis key.
- Tri-state preconditions and persisted evidence requirements.
- Scheduler-triggered `EVIDENCE_DEADLINE` revisions; the scheduler starts a
  run but does not decide hypothesis state or resolution.
- Revision API endpoints expose persisted history and diffs.

### Product validation

- Product-resolution harness with clean-baseline and prehistory guards.
- Revision scheduling, immutable product artifacts, transition proofs, and
  global safety counters.
- T5 evidence and T6 rule replay ablations in the evaluation layer.
- Verified EnvPatch action for controlled product scenarios.
- M19-7.1 CPU-normal calibration for PR-02: `order-service` CPU limit
  `200m`, with request unchanged at `100m`; three valid measured peaks were
  0.012346, 0.003401, and 0.003745, all below the 0.10 normality target.

## Accepted live result — G19.6

A fresh, dedicated Kind product cluster ran with real service traffic and a
real `OrderErrorRateHigh` alert. The accepted revision path was:

```text
R1 INITIAL = AMBIGUOUS
→ product-created STATUS_CONTINUITY evidence requirement
→ R_early MANUAL = AMBIGUOUS
→ scheduler starts an EVIDENCE_DEADLINE revision
→ persisted A1 RECOVERED evidence eliminates a competing manifestation
→ R2 EVIDENCE_DEADLINE = RESOLVED
```

The requirement was superseded at the early revision and satisfied by the
final revision. Persisted A1 recovery evidence made the competing manifestation
hypothesis root-ineligible, leaving the protocol root actor as leader. No
synthetic diagnosis, root, or hypothesis state was injected. Model decision
authority was not required; deterministic RCA rules made the resolution
choice. This was one bounded product-path validation, not a generalization or
accuracy benchmark.

## Validation

PR #2 final validation:

- `make check`: **1,922 passed, 22 skipped**; Ruff passed, formatting passed
  (418 files already formatted), and mypy passed (319 source files).
- `make test-pg`: **22 passed**.

## Explicitly superseded validation work

### M19-7.2 / PR-02N — synthetic CPU pressure

**SUPERSEDED.** The frozen `requests.cpu=100m`, `limits.cpu=100m` candidate
produced a raw throttle peak of **0.029289**, below the synthetic target of
**≥ 0.40**. No CPU-burn fault was added, traffic profile was not changed, the
request was not lowered, A2 thresholds were not changed, and the workload was
not tuned to cross the benchmark target. N2 is not claimed PASS.

### M19-7.4 / PR-03N — synthetic memory ballast

**SUPERSEDED.** The synthetic memory-ballast validation path is no longer an
active product-validation target. Future resource-pressure validation should
prefer realistic fixed misconfiguration or naturally occurring evidence over
tuning artificial workload pressure to satisfy a benchmark threshold. N3 is
not claimed PASS. This does not remove the existing workload ballast code or
change A2 semantics, thresholds, or `ResourcePressure` behavior.

## Known product gaps after M19

1. **RCA breadth:** Legitimate incidents can remain `AMBIGUOUS`; positive root
   confirmation is limited by the evidence and rules currently available.
2. **Live signal wiring:** `LiveSource.traffic_observations()` and
   `LiveSource.trace_observations()` currently return `[]`. Base live RCA does
   not yet consume bounded traffic/trace observations through `LiveSource`;
   this does not mean the project has no Tempo/investigation support.
3. **Durability:** The local reference PostgreSQL is a one-replica Deployment
   with no durable volume, volume mount, or PVC configured. Evidence durability
   is therefore coupled to the ephemeral container/Pod storage lifecycle and
   is not production-grade. No claim is made here about empirically observed
   data loss on restart.
4. **Retention and replay:** Retention is opt-in. Event rows can be deleted by
   age horizon; lifecycle rows can be deleted under age and sequence/Event
   guards; old ObjectVersion rows can be deleted while each object's latest
   version is retained. Current retention logic does not protect rows solely
   because a retained `run_evidence_manifest` references them. A replay whose
   required evidence was removed fails to load; the replay lifetime contract
   remains future work.
5. **Portability:** Some observability queries and signal mappings remain
   demo-workload-specific.
6. **Deployment:** General external-cluster installation packaging and
   high-availability deployment are not complete.

## Next direction

Post-M19 work shifts from proving the correctness foundation to increasing real
RCA capability and operational product readiness. Immediate investigation
priorities are:

1. Understand why diagnoses remain unresolved and classify the ambiguity
   sources.
2. Wire bounded live trace and traffic evidence into the base RCA path.
3. Provide durable evidence storage and replay-aware retention.
4. Improve operator-visible revision and explanation surfaces.
5. Package deployment for external clusters.
