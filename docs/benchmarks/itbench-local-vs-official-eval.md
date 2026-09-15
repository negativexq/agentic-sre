# ITBench local versus official evaluation

The pinned evaluator revision `14f026fc9cc348c4ecec5ab32714de954c95c1b1`
was run against immutable E4, E5, and E6 prediction directories. No agent was
re-executed and no E7 agent ledger was used.

| execution | local deterministic macro F1 | official ROOT_CAUSE_ENTITY mean F1 |
|---|---:|---:|
| ITB-E4 | 0.01905 | 0.22222 |
| ITB-E5 | 0.00000 | 0.00000 |
| ITB-E6 | 0.02857 | 0.76190 |

The official evaluator output is preserved in the corresponding JSON files.
Its aggregate denominators differ from the fixed 35-scenario deterministic
denominator when null scores are returned, so the values are not numerically
interchangeable with local macro scores. The raw scenario-level results are
the cross-check surface.

Official judge accounting is separate from agent accounting. It used
`gpt-4-turbo`; no agent ledgers were modified.
