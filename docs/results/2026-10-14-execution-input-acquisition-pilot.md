# Execution-input acquisition: interrupted paired pilot

No benefit attributable to the new execution-input routes was demonstrated.
17 of 40 planned runs completed; eight complete pairs are available. The offline
process was interrupted after the user objected to prolonged work without a
concrete improvement. The incomplete Scenario-12 control run is excluded.
This is not a completed DEV10 benchmark or an independent HOLDOUT result.

## Frozen comparison

Engine 2.23.0 at `f730757`, fixed DEV10, full/bounded initial views, default
budgets, alternating on/off order. The control disables only execution-input
goal projection; both arms retain the same resolver and frontier contracts.
No labels, TEST25, HOLDOUT23, live services or LLM calls were used. No engine
code, causal rule, threshold or budget was changed from these outcomes.

Protocol, archived runner and its SHA256, completed raw predictions and metrics
are retained in `.local/diagnostics/execution-input-acquisition-dev10-20261014/a/`.
`termination.json` records the stop; `analysis.json` includes all 17 completed
runs and the eight complete comparisons. The archived runner remains authoritative;
the workspace runner has an additional equivalent non-None guard for type checking.

## Observed result

| Measure | Completed pilot |
| --- | ---: |
| Completed runs / planned | 17 / 40 |
| Complete on/off pairs | 8 |
| Turns offering a RULE_INPUT candidate | 0 |
| Reads selected through RULE_INPUT | 0 |
| Complete pairs with equal RCA decision digest | 8 / 8 |
| Complete pairs with equal actions, observations, ledger, counts and stop | 8 / 8 |
| Feature-attributable RESOLVED gain demonstrated | 0 |

Scenario-4, 7, 8 and 11 completed both arms in both seed modes. Scenario-12
completed only the full-source on arm and is not a paired effectiveness result.
Final outcomes across the unbalanced 17 runs were nine AMBIGUOUS and eight
INSUFFICIENT_EVIDENCE. These counts must not be used as an on/off comparison.

## Why this does not establish an effective research behavior

The new contracts cover constituent rollout/failed-rollout/quota inputs only
after a positive change-onset-path/v2 origin witness exists. In the inspected
complete cases, positive origins were ConfigMap changes (Scenario-4 and 8), or
absent (Scenario-7 and 11); Scenario-11 records causal onset as unknown. A generic
NOT_FIRED execution record alone does not identify an acquirable missing input.
No eligible new route was offered, so this pilot cannot establish that the new
selector retrieves the right input when such a route exists. It also cannot
establish universal inapplicability across the uncompleted DEV10 cases.

The implementation is narrower than the intended general behavior: it does not
yet turn missing origin, onset, mechanism or relation evidence into targeted
research. Lowering positive-origin requirements from these results would not
solve that gap. Any next extension needs an explicit existing-rule input contract,
a real source capable of delivering that input, and an activation check before a
long effectiveness measurement; scenario identifiers and benchmark labels must
not determine routing.

## Cost and validation

The pilot ran for about 35 minutes including preparation and interrupted work.
Full-source steps can overshoot the 120-second between-step operational budget;
this is local computation, not measured provider or model billing. No latency
improvement, accuracy, false-resolution or false-exclusion rate is claimed.
New-reference and normalized-finding counters are acquisition proxies; exact
predicate receipt was not graded and is not equivalent to rule satisfaction.

Fourteen execution-input/measurement tests passed, and ten measurement tests
passed separately; the sets overlap. Ruff and mypy passed for the new runner and
its tests. These checks establish instrumentation/control invariants, not a
measured improvement in investigations.
