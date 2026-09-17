# Evaluation methodology

The product is measured, not tuned, on benchmark data. This file is the
contract every reported number follows.

## Data

| Set | Source | Use |
| --- | --- | --- |
| ITBench-Lite dev | 10 scenarios in [`itbench_split.json`](itbench_split.json) | Development and tuning; ground truth may be inspected |
| ITBench-Lite test | 25 scenarios in [`itbench_split.json`](itbench_split.json) | Reported once per frozen release; never used for tuning |

The split is deterministic (`sha256("agentic-sre-split-v1:<id>")`, first ten
are dev) and was committed before the diagnosis engine was written.

## Rules

1. Diagnosis never reads ground truth. Grading loads it only after all
   predictions for a run are written.
2. Test-set numbers are produced only from a tagged release with a frozen
   configuration, and every such run is kept.
3. Every report shows the stage funnel (root cause in catalog, in top 3,
   diagnosed, correct) so a change can be attributed to a stage.
4. Verified and unverified diagnoses are scored separately and together.
5. Baselines are reported next to the full system: deterministic engine only,
   and the retired E10 agent (macro F1 0.0).
6. Runs that use a live model record provider, model, calls, and tokens.
   Offline runs use no model and say so.

## Disclosure

Before this split existed, the retired E10 predictions were compared with
ground truth for all 35 scenarios (failure analysis in the
`archive/experiments-2026-09` tag). That analysis showed which entity kinds
are common root causes (feature-flag ConfigMaps and Chaos Mesh objects). The
engine's rules are generic Kubernetes change and fault signals, but test-set
results should be read with this prior exposure in mind.
