# Evidence-directed research: paired DEV10 measurement

The question-recording layer works, but this run shows no improvement in acquisition
over the existing deterministic selector. Its current value is explicit research
scope and traceability. Better evidence-directed query behavior remains development work.

## Protocol and artifacts

Engine **2.20.0**, engine commit `94811e8`, pinned ITBench-Lite revision
`d0916b08ba421ce5e672e9ad68aa947d938dfef0`, existing DEV10 scenarios:
4, 7, 8, 11, 12, 17, 21, 29, 34 and 91. No labels, TEST25 or HOLDOUT23 were read.

Each scenario ran with `FULL_SOURCE` and `BOUNDED_INITIAL_VIEW`, with research
metadata on and off: **40 runs, 20 paired comparisons**. Arms alternated order
per scenario/seed. Metadata-off patches only the question projection functions in
this measurement process; it is not a product switch or a different resolver.
Default budgets stayed at six turns, eight total tool calls, two calls per gap and
120 seconds. No LLM or live telemetry provider was used.

Input parsing was warmed once per scenario and recorded separately. Each arm's
time includes its actual graph execution. There was only one pair per scenario/seed;
these local timings are descriptive, not production latency estimates.

Artifacts: `.local/diagnostics/evidence-directed-research-dev10-20261014/a/`.
The protocol was written before execution. Raw predictions, per-run timings and
the pilot runner are retained, together with the runner SHA256. `analysis.json`
is the final report, including independently recomputed comparisons, read bindings
and read-only blocker projections. Raw `summary.json` is preserved.

The pilot's acquisition counters were conditional on question-tagged reads, so
metadata-off incorrectly displayed zero acquisition counters despite identical
observations. Analysis version `research-acquisition.v2` counts actual acquisition
in both arms independently of annotation presence. It also reports matched initial
frontier answer transitions. No prediction, timing or raw artifact was changed.

## Acquisition and recording

Values below are for the metadata-on arm. Acquisition outcomes are identical in
metadata-off controls. The two seed modes are separate experiments, not independent
incident populations to pool for accuracy.

| Metric | FULL_SOURCE, 10 scenarios | Bounded initial view, 10 scenarios |
|---|---:|---:|
| Executed tool reads | 57 | 60 |
| Reads linked to an exact research question | 57/57 | 60/60 |
| Reads bringing new evidence references | 1 | 26 |
| Reads producing new normalized findings | 0 | 18 |
| NO_DATA reads | 30 | 29 |
| Nonempty reads returning only known evidence (M14 duplicate count) | 26 | 5 |
| Previously open, matched frontier IDs becoming answered | 0 | 0 |
| Current gap records represented in research output | 542/542 | 422/422 |
| Selected-read/query binding failures | 0 | 0 |
| Rejected actions | 0 | 0 |

All **20/20** paired comparisons match actions, observations, ledger, tool count,
stop reason and epistemic diagnosis digest. All 117 executed metadata-on reads are
bound to their exact selected query/target/capability, and all current gap IDs are
represented in final research records.

The sole FULL_SOURCE acquisition with new refs was Scenario-8's resource-pressure
read on the collector Pod: two additional CPU/memory provenance refs, with no new
normalized finding. Four FULL_SOURCE observations had a SUPPORTS outcome, but none
produced a new finding. A supporting observation is not necessarily a new proof.

The bounded experiment recovers observations deliberately withheld from its starting
view. It shows that existing acquisition can recover data; it does not show a gain
caused by the new metadata layer. Its controls acquire exactly the same data.

Endpoint matching counts only an initial frontier ID explicitly recorded as answered
later under existing rules. A disappeared gap, a new finding or a changed decision
digest is not counted as complete question closure. A general answered-question count
remains unassessed. This matching does not count questions first introduced later.

M14 reports **55/57 useful calls** in FULL_SOURCE, despite zero new normalized findings
and zero matched frontier closures. Its definition includes structural frontier
exploration. That metric must not be interpreted as new causal-proof acquisition.

## What remains open

The final FULL_SOURCE resolution distribution remains 7 AMBIGUOUS and 3
INSUFFICIENT_EVIDENCE. Bounded starts with 10 INSUFFICIENT_EVIDENCE and ends with
3 AMBIGUOUS and 7 INSUFFICIENT_EVIDENCE, identically in both arms. No accuracy,
False RESOLVED or False Exclusion rate was graded.

Read-only blocker projection of FULL_SOURCE final records:

| Existing condition | Recorded assessment |
|---|---|
| UNIQUE_STRONG_CAUSE | UNSATISFIED in 10/10 |
| WITNESS_SCOPE_COVERED | UNASSESSED in 10/10 |
| NO_UNRESOLVED_RIVALS | UNSATISFIED in 10/10 |
| MATERIAL_FRONTIER_CLOSED | UNSATISFIED in 7/10; SATISFIED in 3/10 |

UNASSESSED is not a failed coverage assessment. A satisfied frontier condition can
mean there are no material questions; it does not certify all dependencies healthy.
These cases are not a cohort with one Kafka-only blocker. Strong initiating-mechanism
proof and unresolved claims remain substantive concerns.

Research availability is local planning metadata, separate from these decision conditions:

| Final research availability | FULL_SOURCE | Bounded |
|---|---:|---:|
| AVAILABLE | 360 | 260 |
| READS_ALREADY_ATTEMPTED | 166 | 143 |
| GAP_BUDGET_EXHAUSTED | 16 | 7 |
| NO_ELIGIBLE_CANDIDATE | 0 | 12 |

All runs ended on global budgets: FULL_SOURCE had eight turn-budget and two wall-time
stops; bounded had ten turn-budget stops. AVAILABLE does not imply another read is
permitted after that stop, or that it would answer the question. No eligible candidate
does not establish whether the deficit is missing telemetry or a missing authority rule.

## Cost

Telemetry observations and executed-read counts match exactly between paired arms.
There were zero model calls and no live provider HTTP requests in this snapshot run.

| Compact serialized InvestigationResult size, summed over 10 runs | Metadata off | Metadata on | Increase |
|---|---:|---:|---:|
| FULL_SOURCE | 44,741,265 bytes | 48,263,751 bytes | 3,522,486 bytes; **7.87%** |
| Bounded | 17,315,894 bytes | 18,866,567 bytes | 1,550,673 bytes; **8.96%** |

These are compact JSON sizes, not provider network traffic, PostgreSQL storage or billing.
Warm graph times summed to 647.94 vs 638.57 seconds for FULL_SOURCE (on/off), and
101.13 vs 106.55 seconds for bounded. The opposite direction between modes and single
pairs do not establish a reliable CPU overhead or performance improvement.

## Next development target

47/57 FULL_SOURCE reads targeted DEPENDENCY_HEALTH, while the unique strong-cause
condition remained unmet in all ten cases. This motivates inspecting whether each
proposed query can supply a concrete missing mechanism fact, rather than simply
matching an uncertainty dimension and a nearby component.

Project existing witness/frontier preconditions into specific evidence needs, distinguish
missing observations from missing identity/binding and missing authority rules, and
choose bounded reads that can supply those needs. Known raw records may still be useful
if they can form a new validated relation; raw novelty alone must not be a hard gate.
Avoid spending more budget on generic reads without a recorded discrimination rationale.
Keep existing resolver authority and UNKNOWN semantics unchanged.

This run exercises the general question layer with the snapshot adapter. It does not
exercise real Tempo log-bound request-pair acquisition or establish independent live
fault-family accuracy. That prior isolated Kubernetes DEV proof remains a separate
measurement. No shared deployment, stored diagnosis or HOLDOUT protocol was modified.

## Reproduction and validation

```sh
.venv/bin/python -m scripts.measure_evidence_directed_research \
  --output .local/diagnostics/evidence-directed-research-dev10-20261014/new-run

.venv/bin/python -m scripts.measure_evidence_directed_research \
  --output .local/diagnostics/evidence-directed-research-dev10-20261014/a --reanalyze
```

The second command only reads stored results and writes derived analysis/projections.
Fresh runs also generate `analysis.json` automatically after the raw run summary.
Measurement tests cover annotation-independent acquisition counts, query divergence
with unchanged decisions/call counts, explicitly recorded frontier transitions, refusal
to count disappearance as closure, and preservation of raw artifacts without rerunning
the engine. The focused measurement/M14 metric suite passed **8 tests**. Ruff,
formatting and mypy checks passed.
