# v1.0.1 results

Commit `5ee06550` (tag `v1.0.1`), clean tree, deterministic engine, no model.

| Metric | Result |
| --- | ---: |
| Scenarios | 25 |
| Macro F1 | 0.680 |
| Root cause ranked first | 68% |
| Root cause in top 3 | 80% |
| VERIFIED count | 23 |
| VERIFIED correct | 16 |
| VERIFIED coverage | 92.0% |
| VERIFIED accuracy | 69.6% |
| Model calls | 0 |
| Runtime | 60.8 s |

Prediction never read ground truth (`ground_truth_read_during_prediction=false`).
The dataset revision and sealed prediction manifest are recorded in
`test/manifest.json`; `test/seal.json` protects that manifest and the prediction
files, while `test/report-seal.json` protects the derived grading reports.
Dataset revision: `d0916b08ba421ce5e672e9ad68aa947d938dfef0`; SRE snapshot:
`v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7`.

The annotated `v1.0.1` tag intentionally remains at the release-code commit
`5ee06550`. The immutable tag snapshot contains an earlier intermediate result
publication whose manifest recorded `00dd5bc`; the final refreshed result on
`main` was published afterward as a child documentation/artifact commit and
records the tagged code SHA `5ee06550`. The tag was not moved or rewritten.

The v1.0.1 root-cause and confidence predictions are unchanged from the
published v1.0.0 test results for all 25 scenarios. The new report names the
previous coverage-weighted metric explicitly and adds confidence-class
precision and share-of-predictions reporting.

The ITBench-Lite test set has prior-exposure caveats documented in
[`evals/README.md`](../../README.md).
