# v1.1.0 results

Commit `01d94d5` (tag `v1.1.0`), clean tree, deterministic engine, no model.

| Metric | Result |
| --- | ---: |
| Scenarios | 25 |
| Macro F1 | 0.680 |
| Top-1 | 68% |
| Top-3 | 76% |
| Root cause observable | 88% |
| VERIFIED count | 10 |
| VERIFIED correct | 8 |
| VERIFIED coverage | 40.0% |
| VERIFIED accuracy | 80.0% |
| LIKELY count | 15 |
| UNVERIFIED count | 0 |
| Model calls | 0 |
| Runtime | 147.1 s |

Prediction never read ground truth (`ground_truth_read_during_prediction=false`).
The dataset revision and sealed prediction manifest are recorded in
`test/manifest.json`; `test/seal.json` protects the manifest and prediction
files, while `test/report-seal.json` protects the derived grading reports.
Dataset revision: `d0916b08ba421ce5e672e9ad68aa947d938dfef0`; SRE snapshot:
`v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7`.

The release introduced the bounded investigation layer. The deterministic
baseline above is published separately from the later real-model investigation
evaluation; the v1.1.0 agent run was blocked by a checkpoint serialization
defect and is documented in `docs/results/v1.1.0-test-evaluation.md`.

The ITBench-Lite test set is frozen regression evidence with prior-exposure
caveats documented in [`evals/README.md`](../../README.md).
