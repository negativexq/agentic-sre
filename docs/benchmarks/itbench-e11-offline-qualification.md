# E11 offline retrieval qualification

All candidate/catalog outputs were built without GT and frozen before post-hoc grading. No provider was constructed.

- scenarios: 35
- provider invocations: 0
- build GT accesses: 0
- frozen outputs SHA256: `beb1cc46384a9ad13d56eb6d445e19dfac2e4509db220315c71ad4ea08884002`
- telemetry bound: 100 rows/source-file/scenario
- E7 baseline is immutable historical evidence; deltas below are post-hoc comparisons only.

| config | catalog coverage | R@1 | R@3 | R@5 | R@10 | conditional R@10 |
|---|---:|---:|---:|---:|---:|---:|
| R0 | 0.543 | 0.229 | 0.314 | 0.343 | 0.343 | 0.632 |
| R1 | 0.857 | 0.257 | 0.400 | 0.600 | 0.686 | 0.800 |
| R2 | 0.857 | 0.000 | 0.029 | 0.086 | 0.429 | 0.500 |
| R3 | 0.857 | 0.000 | 0.029 | 0.086 | 0.429 | 0.500 |
| R4 | 0.857 | 0.000 | 0.029 | 0.114 | 0.486 | 0.567 |
| R5 | 0.857 | 0.000 | 0.029 | 0.143 | 0.657 | 0.767 |
| R6 | 0.857 | 0.000 | 0.029 | 0.143 | 0.657 | 0.767 |

R5 vs frozen E7: catalog coverage delta +0.514; Recall@10 delta +0.314.
