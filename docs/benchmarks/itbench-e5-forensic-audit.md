# ITBench E5 forensic audit

Derived from immutable E5 result/checkpoint metadata. Ground truth was not loaded.

## Primary failure classes

| Class | Scenarios | Percentage |
|---|---:|---:|
| WRONG_DIAGNOSIS | 6 | 17.14% |
| WIRE_LOCAL_SCHEMA_MISMATCH | 29 | 82.86% |

## Secondary observations

- typed tool failure: 8
- metric/tool timeout: 5
- duplicate/reused request: 2
- request cardinality / local maxItems: 29

## Interpretation

- `MODEL_DECISION_INVALID` at `$.requests` is an adapter/local-schema mismatch, not an RCA score.
- Tool timeout and typed-failure counts are runtime/tool observations; they do not imply ground-truth access.
- Wrong diagnoses and STOP remain model outcomes.
