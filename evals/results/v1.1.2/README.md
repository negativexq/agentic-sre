# Frozen architecture benchmark results

This public summary records the current deterministic Agentic SRE architecture
at commit `8ccce160dda3848b2f4b03ea51ef7663ee14f0b7`. It is the benchmark
evidence for the product claim in the repository README; it is not a prediction
file or a replacement for the repository's sealed release artifacts.

## Results

Two different measurements are reported, and they were produced by different
runs; read the provenance note below before comparing the columns.

- **Ground-truth accuracy** compares the prediction to the published
  ITBench-Lite label. It was measured on the deterministic **full-source**
  path at commit `f96073d`, using the repository's alias-aware grader.
- **FULL_SOURCE agreement** compares the **bounded** prediction at commit
  `8ccce16` to the same engine's own full-source diagnosis. It measures
  information loss under a bounded read budget, not correctness.

The bounded path's own ground-truth accuracy has not been established.

| Evaluation | Scenarios | Scoreable | Ground-truth accuracy | FULL_SOURCE agreement | Model calls |
| --- | ---: | ---: | ---: | ---: | ---: |
| DEV10 development split | 10 | 9 | 9/9 (100%) | 10/10 | 0 |
| **Blind TEST25 holdout** | **25** | **22** | **17/22 (77.3%)** | 21/25 (84%) | **0** |
| **Combined** | **35** | **31** | **26/31 (83.9%)** | 31/35 (88.6%) | **0** |

Four published labels are not matchable by any prediction, because their
entity filters match nothing in their own scenario snapshot: Scenario-29, 23,
38, and 105 (Scenario-38's filter is not a valid expression). They are
excluded from the accuracy denominator and shown in the raw scenario counts.

The frozen TEST25 run used six validated physical reads per incident: 150 tool
calls in total. It acquired 2,226 new evidence references, emitted 244 normalized
Findings, and made 37 decision-relevant calls. It produced 95 `NO_DATA`
observations and zero tool errors.

Confidence calibration against ground truth:

| Confidence | TEST25 correct/total | All 35 correct/total |
| --- | ---: | ---: |
| `VERIFIED` | 8/10 | 13/16 |
| `LIKELY` | 9/15 | 13/19 |

Measured against FULL_SOURCE agreement instead, TEST25 was `VERIFIED` 9/9,
`LIKELY` 11/12, and `UNVERIFIED` 1/4.

## Methodology

- **Benchmark:** ITBench-Lite.
- **Revision:** `d0916b08ba421ce5e672e9ad68aa947d938dfef0`.
- **Dataset manifest SHA256:** `08a5e56dbfa604c59eed8282683d7b3ec224cd7db9303f90618dafd436423eac`.
- **Policy:** deterministic investigation policy; model calls were disabled.
- **Configuration:** six turns, `max_model_calls=6`, eight tool calls, two
  calls per gap, two invalid actions, two no-progress rounds, and a 120-second
  wall-time limit. The deterministic benchmark policy made **0 model calls**;
  `max_model_calls=6` is the generic investigation budget, not observed usage.
- **Engine:** the configured auxiliary event namespace was `chaos-mesh`.
- **Blindness:** TEST25 bounded predictions were persisted and SHA256-hashed
  before any FULL_SOURCE diagnosis was evaluated.
- **Comparison:** for the agreement column, a prediction counted as agreeing
  only when its canonical root entity exactly equaled the separate FULL_SOURCE
  canonical root for the same scenario ID. For the ground-truth column, a
  prediction counted as correct only when its canonical root entity matched
  the scenario's published label under the repository grader.

The frozen prediction artifact SHA256 was:

```text
879cab5f951bacae783160bd214f4962ce6782be38f74cc0870b9ffe67be9819
```

The architecture was frozen before TEST25 grading, and no production code was
changed after the holdout results were visible. The benchmark is evidence on
this pinned 25-scenario set, not a universal production accuracy guarantee.

Historical benchmark runs remain in the sibling versioned directories under
[`evals/results/`](../).
