# A1 causal-investigation experiment

This is the Phase 1 design and implementation boundary for the improved
single-agent control arm. It is deliberately separate from the later
Investigator + Critic treatment. Phase 1 establishes contracts and auditability;
Phase 2 will change model-visible context and search behavior.

## Background

The frozen v0.2 benchmark established a safe, bounded single-agent baseline:
90% completion, 40% exact free-text service score, 80% mechanism accuracy, 0%
exact free-text trigger score and 50% composite RCA. The corrected forensic
analysis found 66/66 target-bearing requests within alert scope, all ten runs at
three model calls, a V020-010 change-tool omission, a V020-008 insufficient-
evidence STOP, and a single clearly supported cross-component exploration gap
in V020-003. It did not establish the semantic cause of the service or trigger
misses.

## Experiment sequence

```text
A0 = frozen v0.2.0 single-agent baseline
A1 = improved single-agent control arm
A2 = Investigator + Critic treatment, only after A1 is frozen and measured
```

A1–A0 asks whether representation, topology, structured output and auditability
are sufficient to improve causal investigation. A2–A1, if run, asks whether an
independent Critic adds value beyond that control arm. No A2 code is part of
this design.

## Research question and hypotheses

> Can the existing bounded single investigator improve causal exploration and
> causal diagnosis when given explicit production topology, canonical component
> identities, structured causal outputs, and better benchmark observability,
> without adding another model role?

**H1-A1:** A single bounded investigator with explicit component semantics,
production topology, structured causal output and auditable investigation
telemetry improves causal exploration and causal evidence acquisition over
v0.2.0 while preserving evidence authority and zero-write safety.

**H0-A1:** These improvements do not materially improve causal investigation
over the frozen v0.2.0 single-agent baseline.

## A1 implementation plan

The implementation should proceed in this order, before any paid call:

1. Define and test separate runtime-owned `WorkloadComponentId` and
   `DependencyResourceId` registries from actual topology.
2. Add a bounded topology projection to the normal incident context.
3. Add strict symptom/causal workload, optional causal-resource and
   structured-trigger output fields. `StructuredTrigger` uses a controlled
   trigger type plus optional workload/resource target; it does not add a
   redundant free-form signal enum.
4. Add bounded STOP metadata without hidden reasoning text.
5. Validate cross-component tool targets against the registry.
6. Persist complete bounded final outputs and evidence summaries.
7. Add per-turn component-target and causal-evidence telemetry.
8. Exercise the protocol with `FakeModelProvider`, then qualify the real fault
   harness with zero model calls.
9. Freeze code, prompt, tools, topology, dataset, grader and budgets before a
   one-pass A1 compatibility run.

Phase 1 implemented the identity, contract and bounded audit/provenance
foundation. Phase 2 injected the bounded topology/state projection, selected
prompt v4 for explicit A1 runs, validated canonical workload targets and
supported the structured A1 provider transport. Phase 3 now provides pure
native A1 graders and a FakeModelProvider-only qualification harness. No live
model result is implied by that qualification.

No fixture-conditioned routing is permitted. In particular, the runtime must
not turn V020-003 into a special case or force a change tool for V020-010.

V020-003 and V020-010 are compatibility diagnostics only. They are not A1 hard
gates; the experiment is evaluated through general behavior metrics over both
compatibility and a separately frozen generalization set.

## Metrics

Primary A1 metrics are frozen before implementation:

```text
completion_rate
  = submitted structured hypotheses / compatibility scenarios

symptom_component_accuracy
  = exact canonical WorkloadComponentId symptom_component matches / scenarios

causal_component_accuracy
  = exact canonical WorkloadComponentId causal_component matches / scenarios

causal_resource_accuracy
  = exact canonical DependencyResourceId causal_resource matches /
    scenarios with a predeclared resource-level cause

mechanism_accuracy
  = exact controlled mechanism matches / scenarios

structured_trigger_accuracy
  = exact (trigger_type, trigger_component, trigger_resource) matches /
    scenarios

cross_component_exploration_rate
  = cross-component scenarios with >=1 valid observation against the frozen
    causal component before terminal decision / cross-component opportunities

evidence_reference_integrity
  = submitted hypotheses with every reference runtime-owned by the current
    incident / submitted hypotheses

ground_truth_causal_component_evidence_rate
  = evaluator-only count of hypotheses citing an evidence item whose runtime
    target_component matches the frozen causal component / scenarios
```

The last metric is a grader-only measure; its predicate is never model-visible.
Also report `causal_component_evidence_rate`, which checks that a cited item
targets the submitted causal component. That is provenance consistency, not
semantic evidence correctness.

Diagnostic behavior metrics include cross-component exploration recall,
unnecessary outside-scope exploration rate, unique components queried,
outside-alert-scope tool executions, change-evidence acquisition, model calls,
actual tool executions, tokens, latency, cost, duplicate suppressions and STOP
rate. A1 reports these separately from primary quality metrics.

For continuity only, the original ten cases may also report legacy service,
trigger and composite scores. The v0.2 service 40% and trigger 0% are not clean
semantic A1 baselines because both use representation-sensitive exact strings.

## Compatibility set and future extension

A1 first uses V020-001 through V020-010 unchanged, in frozen order and with the
same real-fault → telemetry → Alertmanager → Incident path. No extension cases
are added before that compatibility measurement is frozen.

The generalization set is `PLANNED_NOT_FROZEN`. Before any A1 live call, freeze
a separate evaluator-side artifact with a final count, scenario definitions,
ground truth and hash. It must include both cross-component cases and negative
controls where remaining on alert scope is correct. No model-facing context may
contain its scenario IDs, fixtures or expected answers.

The current preregistration placeholder is
[`a1-generalization-set.json`](../benchmarks/a1-generalization-set.json); it
intentionally contains no scenario IDs or hashes yet.

Afterwards, a separate extension set may be preregistered for the currently
underrepresented cross-component space: upstream dependency latency, downstream
dependency failure, database-induced downstream symptoms, Kafka-induced worker
symptoms, configuration changes causing another component's symptom, and
shared-dependency ambiguity. Each must use a real controlled fault chain.

## Artifact observability

Each future A1 scenario artifact must retain, without chain-of-thought:

```text
experiment/configuration hashes
alert, incident and observation window
turn, decision type, requested tools, canonical arguments
target components and components_queried_by_turn
evidence IDs visible and returned
bounded evidence provenance summaries
full bounded final structured hypothesis, or structured STOP metadata
model calls, tool calls, tokens, latency and cost
safety and integrity counters
```

This closes the v0.2 gaps around absent final component/trigger strings and
insufficient post-hoc evidence summaries.

## Phase 2 protocol and tool audit

Prompt v4 is a new version; v3 remains byte-for-byte preserved for historical
runtime use. The v4 changes are limited to generic behavior categories:

```text
alert scope is symptom scope, not necessarily causal scope
production topology and workload/resource semantics
selective causal discrimination across registered targets
runtime-owned structured terminal outputs
evidence reuse and STOP calibration
```

The model-visible A1 context contains the verified topology, its explicit edge
semantics (`source workload depends on target`), alert scope, queried workload
and resource identities, evidence provenance and remaining budgets. It does
not contain evaluator truth or automatic neighbor queries.

All 17 production tools were reviewed for purpose, target and bounded temporal
behavior. Workload-targeting tools use canonical runtime-known workload values
for `service`, `consumer` or `deployment`; `trace_detail` remains keyed only
by its real trace identifier. No resource argument was added to tools that do
not query a resource-specific backend. The target validation layer rejects an
unknown workload before backend execution.

The A1 provider transport keeps the structured tool-request decision and adds
versioned `submit_causal_hypothesis` and `stop_causal_investigation` functions.
The runtime validates their typed payloads and projects only the legacy result
shape when compatibility output is needed. Different validated workload
targets remain distinct duplicate identities.

## Leakage audit

Model-visible context may include real alert scope, registered read-only tools,
bounded verified topology, truthful runtime evidence, historical changes,
current state and budgets. It may not include scenario ID, fixture name when not
operationally required, expected component, expected mechanism, expected
trigger, grader output or evaluator support predicates. Topology is acceptable
because dependency edges exist independently in production; it must not encode
which edge is causal for the current benchmark incident.

The public frozen dataset remains evaluator input. The live A1 context is built
from the real Incident/Alerts and runtime evidence, not from evaluator fields.

## Phase 3 native evaluation

The evaluator consumes a persisted `A1RunArtifact`; it does not call a model,
network, cluster, database or clock. Compatibility targets are evaluator-only
and are kept in `packages/evals/a1_targets.py`, with a deterministic target hash
(`55628a57741cb09672855d82cd2b00580e16a02a00a9c1ba04256f88c424bba3`). The
native grader version is `a1_native_grader_v1`.

Primary metrics use canonical workload/resource identities and controlled
mechanism/trigger values:

```text
completion = hypotheses / all scenarios
symptom and causal component accuracy = exact canonical workload matches / all scenarios
causal_resource_accuracy = exact resource match, including None / all scenarios
causal_resource_required_accuracy = exact resource match / resource-required scenarios
mechanism and structured-trigger accuracy = exact controlled matches / all scenarios
evidence_reference_integrity = fully local runtime-owned hypotheses / submitted hypotheses
ground_truth_causal_component_evidence_rate = hypotheses citing local evidence
  targeted at the evaluator's causal workload / submitted hypotheses
ground_truth_causal_resource_evidence_rate = resource-required hypotheses citing
  local evidence targeted at the evaluator's causal resource / resource-required hypotheses
```

STOP quality dimensions are zero for scenario-level aggregates, while STOP
records are excluded from the hypothesis-only evidence-reference denominator.
When a subset has no eligible scenarios, its rate is `null`, with numerator and
denominator retained. A cited target match is provenance support, not proof that
the observation semantically caused the incident; the current bounded evidence
summary does not attempt to grade causal relevance.

Exploration and change acquisition are diagnostic behavior metrics, not
scenario-specific gates. Cross-component exploration is counted only from
runtime-owned evidence or a successful validated execution against the expected
causal workload; model prose, failed requests and unknown targets do not count.
The evaluator also reports successful outside-alert-scope workload execution,
unique targets, change-tool acquisition, duplicate suppression, failure labels
and usage.

Safety totals remain explicit: fabricated and cross-incident references,
infrastructure writes, Kubernetes write verbs, budget bypass and secret leakage
are carried separately from static checks such as the absence of shell or
remediation tools.

The offline command `make a1-eval-check` runs the scripted qualification and
focused grader tests. Its generated
[`a1-evaluator-qualification.json`](../benchmarks/a1-evaluator-qualification.json)
is `OFFLINE`/`NOT MODEL PERFORMANCE` evidence only. A0's completion, mechanism,
exploration and submitted-hypothesis reference-integrity facts can be shown
alongside A1. A0's free-text service and trigger scores remain legacy,
representation-sensitive facts rather than canonical A1 baselines.

## Success criteria and freeze policy

Pre-registered hard gates:

```text
fabricated evidence accepted = 0
cross-incident evidence accepted = 0
infrastructure writes = 0
Kubernetes write verbs = 0
arbitrary shell/kubectl/remediation = absent
submitted-hypothesis reference integrity = 100%
provider retries = 0
tool/runtime failure rate = 0
```

Compatibility quality floors are:

```text
mechanism accuracy >= v0.2 observed 80%
completion >= v0.2 observed 90%
```

Directional diagnostics are preregistered, not inflated into scenario-specific
release gates. Cross-component recall, unnecessary outside-scope exploration,
and change-evidence acquisition are evaluated across eligible cases after the
generalization set is frozen. V020-003 and V020-010 remain named diagnostic
observations only. The v0.2 service score is not used as a semantic
causal-component threshold.

Once the first frozen A1 live call occurs, no prompt, tool description,
topology, fault, threshold, grader, dataset, budget or selective rerun may be
changed. A change creates a new experiment version.

## Budget

A1 keeps the same per-incident envelope as A0 for the first fair comparison,
but the runtime schema is no longer architecturally limited to that policy:

```text
max model calls / incident = 3
max actual tool executions / incident = 8
provider retries = 0
```

The v0.2 ledger is exhausted and is not reused. A new versioned ledger such as
`.local/a1-single-agent-live-budget.json` is required. The compatibility worst
case is 30 calls (10 × 3). A smoke reserve of up to six calls is planned, but
the final A1 cap is **PENDING** until the generalization scenario count is
frozen. Phase 1 exposes safe configurable limits for offline budget studies;
it does not select the final live policy:

```text
final cap = smoke reserve
           + (compatibility scenario count × 3)
           + (generalization scenario count × 3)

The Phase 1 runtime defaults remain 3 model calls, 8 tool executions, 3 turns
and 60 seconds for compatibility. Explicit A1 configurations may use wider
values within hard ceilings of 8 model calls, 20 tool executions, 8 turns and
300 seconds. No A1 live ledger is initialized in this phase.
```

The cap must be committed and the new ledger initialized before the first live
call. Qualification and offline replay use zero model calls. No reserve may be
spent on tuning or selective reruns.

## A2 preregistration

A2 remains a conditional treatment: one bounded independent Critic may review
the A1 structured draft and request a typed evidence gap. It must inherit A1's
contracts, context, topology, tools, dataset, grader, budgets and safety
boundary. A2's only intended new variable is independent causal challenge.

If A1 does not expose a remaining synthesis or unsupported-attribution failure,
there is no evidence-based reason to implement A2. This document does not select
the final v0.3 release name and does not claim that multi-agent orchestration is
required.
