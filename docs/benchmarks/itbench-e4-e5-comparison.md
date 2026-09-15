# ITBench E4 → E5 post-run comparison

This report is derived from durable checkpoints. It does not rerun the investigator or provider.

| Metric | E4 | E5 | Delta |
|---|---:|---:|---:|
| Macro precision | 0.01429 | 0.00000 | -0.01429 |
| Macro recall | 0.02857 | 0.00000 | -0.02857 |
| Macro F1 | 0.01905 | 0.00000 | -0.01905 |
| Entity hits / 35 | 1 | 0 | -1 |
| Diagnosis submissions | 4 | 6 | +2 |
| Zero-entity scenarios | 31 | 29 | -2 |
| TOOL_CALL_LIMIT | 21 | 0 | -21 |
| MODEL_DECISION_INVALID | 6 | 29 | +23 |
| STOP | 4 | 0 | -4 |
| Tool requests | 524 | 114 | -410 |
| Tool executions | 410 | 112 | -298 |
| Duplicate requests | 64 | 2 | -62 |
| Input tokens | 1601206 | 830784 | -770422 |

## Conditional diagnosis metrics

- E4 P(diagnosis)=0.11429; P(correct | diagnosis)=0.25000; P(correct overall)=0.02857
- E5 P(diagnosis)=0.17143; P(correct | diagnosis)=0.00000; P(correct overall)=0.00000

## E5 terminal/validation observations

- terminals: {'SUBMIT_DIAGNOSIS': 6, 'MODEL_DECISION_INVALID': 29}
- decision validation stages: {'DECISION_SCHEMA': 29}
- tool summary statuses: {'TOOL_EXECUTION_FAILURE': 27, 'SUCCESS': 85, 'SKIPPED_DUPLICATE': 2}
- request batch stats: {'count': 38, 'mean': 3, 'median': 3.0, 'max': 3}

Official ITBench judge: NOT RUN.
