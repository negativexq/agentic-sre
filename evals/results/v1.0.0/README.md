# v1.0.0 results

Commit `6d132e3b` (tag `v1.0.0`), clean tree, deterministic engine, no model
calls. The test split was run once after the release tag. Predictions are
sealed; `manifest.json` records the full commit SHA, runtime, and the fact that
ground truth was not read during prediction.

| Run | Split | Scenarios | Macro F1 | Verified-only F1 | Root cause first | In top 3 | Time |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| [Deterministic](test/report.md) | Test | 25 | **0.680** | 0.640 | 68% | 80% | 60.4 s |

The v1.0.0 prediction identity and confidence are unchanged for every
scenario compared with the sealed v0.7.0 test run. The release therefore adds
live-cluster validation and replay/causal correctness without tuning the
benchmark test split. The ITBench-Lite snapshot revision is
`d0916b08ba421ce5e672e9ad68aa947d938dfef0` and its SRE snapshot is
`v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7`.

The benchmark disclosure in [`evals/README.md`](../../README.md) remains in
force: earlier experiments exposed the scenario family to the project, so
these numbers are regression evidence rather than a pristine unseen test.
