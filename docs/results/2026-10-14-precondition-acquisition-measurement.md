# Atomic precondition acquisition: applicability and paired pilot

No acquisition gain was observed in the completed pilot. The new registry was
not exercised: the DEV10 snapshot source cannot supply the capture records needed
for its registered rules. This is an applicability result, not an effectiveness
failure of the rule and not a completed DEV10 performance benchmark.

## Scope and protocol

Engine 2.22.0, engine commit `e8c2869`, the existing pinned DEV10 list and default
budgets. Both arms use the same RCA kernel and deterministic intent policy.
`registry_off` disables only the input-registry functions in the measurement
process; its acquisition is 2.21.0-compatible, but complete historical 2.21.0
output is not reproduced. No labels, TEST25, HOLDOUT23, live provider or LLM was used.

The original protocol planned 40 runs: ten scenarios, full/bounded seed modes,
registry on/off with alternating arm order. It was recorded before execution,
with an archived runner and SHA256. Four Scenario-4 runs completed. After source
applicability was checked, execution was interrupted during Scenario-7; that
incomplete run is excluded. The protocol and completed raw predictions are retained
under `.local/diagnostics/precondition-acquisition-dev10-20261014/a/`, alongside
`termination.json` and derived `analysis.json`. The planned 40 runs were not completed.

The measurement runner now performs a capture-channel check before scheduling
outcome runs. The final audit is under the same parent directory, `c/`: frozen
protocol, archived runner, `applicability.json` and `summary.json`. It checked all
10 DEV scenarios and scheduled zero further outcome runs. The intermediate `b/`
audit is retained as well. These are capability checks, not ten completed diagnoses.

## Why the registry cannot activate here

`SnapshotSource.evidence_coverage_record()` always returns None: no Connector
stream recorded object continuity. `trace_read_record()` also returns None:
there is no captured record of the trace reads. This does **not** mean there are
no raw objects or trace spans. Bounded views and acquired-evidence overlays
preserve those capture boundaries.

The existing unchanged-configuration rule returns without assessment when object
coverage is absent. Its NO_LISTING and NO_WRITE_RECORD conditions therefore do
not reach the registry. The existing declared-unused dependency rule likewise
cannot assess its input conditions without captured trace-read records. All five
currently registered routes depend on these two rules. Both capture channels
were absent in 10/10 audited sources. No continuity/read record was invented to
make the registry applicable.

## Completed Scenario-4 paired pilot

Each row below is **one scenario**, not a DEV10 aggregate. Values are on/off.

| Observation | Full source | Bounded initial view |
| --- | ---: | ---: |
| Tool reads | 3 / 3 | 6 / 6 |
| Direct-input turns offered | 0 / 0 | 0 / 0 |
| Direct-input reads selected | 0 / 0 | 0 / 0 |
| Reads bringing new provenance | 0 / 0 | 6 / 6 |
| Reads producing normalized findings | 0 / 0 | 2 / 2 |
| Matched initial open frontier IDs answered | 0 / 0 | 0 / 0 |
| Known-only nonempty reads | 3 / 3 | 0 / 0 |
| NO_DATA reads | 0 / 0 | 0 / 0 |
| Final resolution | AMBIGUOUS / AMBIGUOUS | AMBIGUOUS / AMBIGUOUS |
| Stop | WALL_TIME_EXHAUSTED / same | TURN_BUDGET_EXHAUSTED / same |

Both pairs match actions, observations, ledger, tool count, stop reason and RCA
epistemic digest. The on arm executed nine reads; zero were selected via the new
DIRECT route. Bounded recovered data withheld from its seed, equally in both
arms; this is not a benefit attributable to the registry.

Final records contain 75 unmapped deficits in full mode and 54 in bounded mode,
identically in both arms. These are per-rule/per-hypothesis records, not 75/54
independent resolution blockers. Full-mode reasons include missing mechanism
execution and absent rollout/quota incident witnesses, alongside missing onset
capability and symptom linkage. Generic history availability does not discharge
those requirements.

## Cost and limits

Full-mode graph execution took 141.40 / 141.72 seconds; bounded took 7.40 / 8.29
seconds. Source parsing was warmed once (42.76 seconds), separately from the arms.
The full-mode 120-second operational budget is checked between steps, so a long
step can overshoot it. One pair per mode does not establish a latency or speed gain.
Interrupted Scenario-7 work is additional discarded pilot work, not part of these
per-run timings. Observation bytes and tool counts match; no live query or model
billing cost was measured. All model-call counts were zero.

No root-cause correctness, False RESOLVED or False Exclusion rate was graded.
An acquired listing would establish only that an input was obtained, not complete
rule satisfaction. The runner's counters and applicability gate are covered by
focused tests; they retain this separation.

## Next measurement

Freeze a development capture that includes the actual required coverage/read
records and a recorded input deficit, then repeat the on/off comparison with
unchanged engine rules and budgets. Capture continuity must be independently
recorded; it cannot be reconstructed from snapshot density or assigned by an
evaluation adapter. HOLDOUT23 remains untouched.

Before expanding the registry, map the actual execution/effect requirements from
their existing rule contracts to capable observations. The pilot's counts are
for diagnosis, not permission to add scenario exceptions or loosen thresholds.
