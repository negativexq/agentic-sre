# v0.7.0 results

Commit `8f1f6861` (tag `v0.7.0`), clean tree, deterministic engine, no model
calls. Each directory holds sealed predictions, manifest, seal, and report.

| Run | Split | Scenarios | Macro F1 | Verified-only F1 | Root cause first | In top 3 | F1 where GT is observable | Time |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| [Deterministic](test/report.md) | Test | 25 | **0.680** | 0.640 | 68% | 80% | 0.773 | 61 s |
| [Deterministic](dev/report.md) | Dev (tuned on) | 10 | 0.900 | 0.800 | 90% | 90% | 1.000 | 24 s |

Both splits were run once, from the tag. Scores are unchanged from
[v0.5.0](../v0.5.0/README.md): every prediction on both splits is identical,
scenario for scenario.

## What changed and why the score didn't move

This release starts the causal-topology work: `Topology.distance()` treats
every structural edge as undirected, so two unrelated workloads that merely
share a NetworkPolicy or a ConfigMap could appear "close" to each other and
pick up a false link to an alerting component.

`Topology.causal_distance()` and `causal_reachable()` add a guard: once a
graph walk reaches a NetworkPolicy restricting more than 3 pods, or a
ConfigMap used by more than 3 workloads, it may not continue past that node to
a third, unrelated one. It may still explain the node's own direct targets
(a policy is still evidence for the pods it actually restricts). Ranking now
uses `causal_distance` everywhere it previously used `distance`.

A synthetic regression test (`test_container_failure_behind_a_shared_policy_is_not_linked_to_a_sibling`
in `tests/unit/rca/test_engine.py`) reproduces the exact failure mode: four
services behind one broad NetworkPolicy, one of them crashing, another
alerting. Without the fix, the crash was wrongly reported as "linked" to the
alert; with it, it correctly is not. None of ITBench-Lite's 35 scenarios
happen to contain this shape of shared-infrastructure fan-out, so the score
is unchanged — this is a robustness fix for clusters with more shared
policies and config than the benchmark's, not a benchmark-driven one.

## Also in this release

- `Topology.causal_distance()`/`causal_reachable()` are additive: `distance()`
  and `reachable()` still exist unchanged, for anything that still needs an
  undirected walk (e.g. finding a pod's own workload).
- v0.6.0's journal, lifecycle, and temporal-window fixes are included
  (no score effect either, by design — see
  [v0.6.0's commit range](https://github.com/negativexq/agentic-sre/compare/v0.5.0...v0.6.0)).

Test misses and their causes are unchanged from [v0.5.0](../v0.5.0/README.md).
