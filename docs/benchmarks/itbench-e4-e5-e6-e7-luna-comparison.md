# ITBench E4–E7 Luna comparison

All canonical external values below use pinned evaluator revision
`14f026fc9cc348c4ecec5ab32714de954c95c1b1`, metric `ROOT_CAUSE_ENTITY`, and
judge `gpt-5.6-luna` through compatibility profile
`itbench_luna_judge_compat_v1` (SHA256
`104483524ebcd4faa824aed4a2982d9c7b01a9791b90d3e5acd464d06b08307f`). The
profile explicitly uses temperature 1, zero provider retries, and one
inference attempt per case.

| execution | Luna official mean F1 | local deterministic macro F1 | diagnosis coverage |
|---|---:|---:|---:|
| E4 | 0.01905 | 0.01905 | not durably available in this summary |
| E5 | 0.00000 | 0.00000 | not durably available in this summary |
| E6 | 0.11765 | 0.02857 | not durably available in this summary |
| E7 | 0.17172 | 0.08571 | 17/35 = 0.48571 |

E7 is complete and valid: 35 unique official scenarios ran once, in the
frozen order, with 171 agent outbound attempts. Its remaining agent-ledger
credit is intentionally unused (`171/175`; 4 remaining), and no reruns were
performed. The E7 local diagnostic metrics are computed from persisted
checkpoints with a fixed denominator of 35: micro precision `0.14286`, micro
recall `0.08571`, micro F1 `0.10714`, and entity hit rate `3/35`.

E7's Luna external mean F1 exceeds E6's Luna value by `0.05407` (relative
change approximately `45.96%`). This is a same-judge comparison; the
historical GPT-4-Turbo values are reported separately in the judge-model
comparison and are not used for the E6→E7 primary claim.

The full machine-readable scenario rows, terminal counts, usage totals,
tool-category counts, and judge metadata are in
`itbench-e4-e5-e6-e7-luna-comparison.json`.
