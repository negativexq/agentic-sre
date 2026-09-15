# ITBench E7 → E8 comparison

E8 is the first and only frozen 35×1 execution of the repaired pre-official
runtime `e474db9432b17fc854873fa8458a3b9b0de8d49a`. E7 and E8 use the same
Luna judge identity: evaluator `14f026fc9cc348c4ecec5ab32714de954c95c1b1`,
`gpt-5.6-luna`, and `itbench_luna_judge_compat_v1`.

| Metric | E7 | E8 | Delta |
|---|---:|---:|---:|
| Luna official ROOT_CAUSE_ENTITY F1 | 0.171717 | 0.028571 | -0.143146 |
| Official denominator | existing E7 artifact | 35 | disclosed |
| Local fixed-35 macro F1 | 0.085714 | 0.000000 | -0.085714 |
| Diagnosis coverage | 17/35 (48.57%) | 7/35 (20.00%) | -28.57 pp |
| MODEL_DECISION_INVALID | 13 | 21 | +8 |
| STOP | 4 | 7 | +3 |
| TOOL_CALL_LIMIT | 1 | 0 | -1 |
| Agent model calls | 171 | 145 | -26 |
| Calls/scenario | 4.89 | 4.14 | -0.74 |
| Input tokens | ~2,244,100 | 918,575 | -59.07% |
| Semantic tool requests | 385 | 329 | -14.55% |
| Semantic tool executions | 369 | 325 | -11.92% |
| Typed failures | 39 | not durably reported in final aggregate | NDR |
| Timeouts | 0 | 0 | unchanged |
| Scenario-105 first-turn context | ~65,135 chars | 10,590 chars | -83.74% |

The result is valid but regressed on the primary quality criterion. Efficiency
improved materially, but the higher invalid-decision rate and lower diagnosis
coverage prevent a quality improvement claim.

## Classification

`QUALITY_DOWN_EFFICIENCY_UP`

`ITB_E8_COMPLETE_EFFICIENCY_IMPROVED_QUALITY_REGRESSED`

No E9 was started. No E8 scenario was rerun.
