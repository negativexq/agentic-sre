# Frozen architecture benchmark results

This public summary records the current deterministic Agentic SRE architecture
at commit `8ccce160dda3848b2f4b03ea51ef7663ee14f0b7`. It is the benchmark
evidence for the product claim in the repository README; it is not a prediction
file or a replacement for the repository's sealed release artifacts.

## Results

| Evaluation | Scenarios | Exact FULL_SOURCE root agreement | Model calls |
| --- | ---: | ---: | ---: |
| DEV10 development split | 10 | 10/10 | 0 |
| **Blind TEST25 holdout** | **25** | **21/25 (84%)** | **0** |
| **Combined** | **35** | **31/35 (88.6%)** | **0** |

The TEST25 run used six bounded physical reads per incident: 150 tool calls in
total. It acquired 2,226 new evidence references, emitted 244 normalized
Findings, and made 37 decision-relevant calls. It produced 95 `NO_DATA`
observations and zero tool errors.

Confidence calibration on TEST25 was:

| Confidence | Correct | Total |
| --- | ---: | ---: |
| `VERIFIED` | 9 | 9 |
| `LIKELY` | 11 | 12 |
| `UNVERIFIED` | 1 | 4 |

## Methodology

- **Benchmark:** ITBench-Lite.
- **Revision:** `d0916b08ba421ce5e672e9ad68aa947d938dfef0`.
- **Dataset manifest SHA256:** `08a5e56dbfa604c59eed8282683d7b3ec224cd7db9303f90618dafd436423eac`.
- **Policy:** deterministic investigation policy; model calls were disabled.
- **Configuration:** six turns, six model-call budget, eight tool-call budget,
  two calls per gap, two invalid actions, two no-progress rounds, and a
  120-second wall-time limit.
- **Engine:** the configured auxiliary event namespace was `chaos-mesh`.
- **Blindness:** TEST25 bounded predictions were persisted and SHA256-hashed
  before any FULL_SOURCE diagnosis was evaluated.
- **Comparison:** a prediction counted as correct only when its canonical root
  entity exactly equaled the separate FULL_SOURCE canonical root for the same
  scenario ID.

The frozen prediction artifact SHA256 was:

```text
879cab5f951bacae783160bd214f4962ce6782be38f74cc0870b9ffe67be9819
```

The architecture was frozen before TEST25 grading, and no production code was
changed after the holdout results were visible. The benchmark is evidence on
this pinned 25-scenario set, not a universal production accuracy guarantee.

Historical benchmark runs remain in the sibling versioned directories under
[`evals/results/`](../).
