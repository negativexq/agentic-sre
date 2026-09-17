# v1.0.2 results

Commit `30f1ad7` (tag `v1.0.2`), clean tree, deterministic engine, no model.

| Metric | Result |
| --- | ---: |
| Scenarios | 25 |
| Macro F1 | 0.680 |
| Root cause ranked first | 68% |
| Root cause in top 3 | 80% |
| VERIFIED count | 9 |
| VERIFIED correct | 8 |
| VERIFIED coverage | 36.0% |
| VERIFIED accuracy | 88.9% |
| LIKELY count | 16 |
| UNVERIFIED count | 0 |
| Model calls | 0 |
| Runtime | 145.9 s |

Prediction never read ground truth (`ground_truth_read_during_prediction=false`).
The dataset revision and sealed prediction manifest are recorded in
`test/manifest.json`; `test/seal.json` protects the manifest and prediction
files, while `test/report-seal.json` protects the derived grading reports.
Dataset revision: `d0916b08ba421ce5e672e9ad68aa947d938dfef0`; SRE snapshot:
`v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7`.

The official exact benchmark score is unchanged from v1.0.1. The release
changes the confidence contract: late or weakly initiating evidence is now
reported as `LIKELY` instead of `VERIFIED`. Root-cause ranking changed only
for Scenario-38, where the normalized HPA finding replaced a Pod candidate;
that ground-truth cause is not observable in the published snapshot, so the
official score remains unchanged.

The ITBench-Lite test set is frozen regression evidence with prior-exposure
caveats documented in [`evals/README.md`](../../README.md).
