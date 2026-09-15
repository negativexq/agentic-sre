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

The table above is the historical evaluator series and used `gpt-4-turbo`.
It is preserved as a non-standard historical measurement. Official judge
accounting is separate from agent accounting; no agent ledgers were modified.

## Luna canonical rescoring

The same frozen predictions were subsequently evaluated with the pinned
evaluator revision and the repository-owned `itbench_luna_judge_compat_v1`
profile. The source revision was not modified; the compatibility profile
sets `temperature=1`, `provider_max_retries=0`, and one evaluator inference
attempt per case because those provider parameters are required by the Luna
execution path.

| execution | Luna ROOT_CAUSE_ENTITY mean F1 | local deterministic macro F1 |
|---|---:|---:|
| ITB-E4 | 0.01905 | 0.01905 |
| ITB-E5 | 0.00000 | 0.00000 |
| ITB-E6 | 0.11765 | 0.02857 |
| ITB-E7 | 0.17172 | 0.08571 |

These Luna values are the canonical project external-evaluator series. They
must not be treated as the same measurement instrument as the historical
GPT-4-Turbo values. Detailed per-scenario differences are recorded in
`itbench-judge-model-comparison.json` and
`itbench-e4-e5-e6-e7-luna-comparison.json`.
