# ITBench E4 → E5 → E6 comparison

Derived from durable result/checkpoint artifacts; official judge not run.

| Metric | E4 | E5 | E6 |
|---|---:|---:|---:|
| Macro F1 | 0.019047619047619046 | 0.0 | 0.02857142857142857 |
| Entity hits / 35 | 1 | 0 | 1 |
| Diagnosis coverage | 4 | 6 | 30 |
| MODEL_DECISION_INVALID | 6 | 29 | 2 |
| TOOL_CALL_LIMIT | 21 | 0 | 0 |
| STOP | 4 | 0 | 3 |
| Tool requests | 524 | 114 | 316 |
| Tool executions | 410 | 112 | 238 |
| Duplicate actions | 64 | 2 | 78 |
| Input tokens | 1601206 | 830784 | 2154863 |

## Conditional diagnosis metrics

- E4: P(diagnosis)=0.11429; P(correct | diagnosis)=0.25000; P(correct overall)=0.02857
- E5: P(diagnosis)=0.17143; P(correct | diagnosis)=0.00000; P(correct overall)=0.00000
- E6: P(diagnosis)=0.85714; P(correct | diagnosis)=0.03333; P(correct overall)=0.02857

## Validity note

E5 and E6 are engineering iterations on the same public scenarios after the E4 baseline; neither is untouched held-out generalization.
