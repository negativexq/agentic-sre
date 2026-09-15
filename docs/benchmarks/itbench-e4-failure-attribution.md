# ITB-E4 failure attribution

This is a post-hoc forensic report over the immutable E4 checkpoints. It does not alter or reinterpret E4.

| Primary class | Scenarios |
|---|---:|
| Tool-budget exhaustion | 21 |
| Decision/schema failure | 5 |
| Evidence reference failure | 1 |
| Voluntary STOP | 4 |
| Diagnosis submitted, wrong cause | 3 |
| Diagnosis submitted, partial correctness | 1 |

| Scenario | Terminal | Primary class | Requests | Executions | Duplicates | Typed failures | Validation |
|---|---|---|---:|---:|---:|---:|---|
| Scenario-1 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 16 | 12 | 3 | 0 | — |
| Scenario-2 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 15 | 12 | 0 | 0 | — |
| Scenario-4 | SUBMIT_DIAGNOSIS | Diagnosis submitted, wrong cause | 14 | 12 | 2 | 1 | — |
| Scenario-5 | STOP | Voluntary STOP | 12 | 12 | 0 | 0 | — |
| Scenario-6 | MODEL_DECISION_INVALID | Evidence reference failure | 12 | 12 | 0 | 0 | FABRICATED_EVIDENCE_REFERENCE |
| Scenario-7 | STOP | Voluntary STOP | 12 | 12 | 0 | 0 | — |
| Scenario-8 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 16 | 12 | 2 | 0 | — |
| Scenario-9 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 13 | 12 | 0 | 0 | — |
| Scenario-11 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 15 | 12 | 0 | 0 | — |
| Scenario-12 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 15 | 12 | 0 | 0 | — |
| Scenario-13 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 15 | 12 | 0 | 0 | — |
| Scenario-14 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 18 | 12 | 0 | 0 | — |
| Scenario-15 | STOP | Voluntary STOP | 14 | 12 | 2 | 0 | — |
| Scenario-16 | STOP | Voluntary STOP | 13 | 12 | 1 | 0 | — |
| Scenario-17 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 14 | 12 | 0 | 0 | — |
| Scenario-18 | SUBMIT_DIAGNOSIS | Diagnosis submitted, partial correctness | 15 | 12 | 3 | 0 | — |
| Scenario-19 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 14 | 12 | 0 | 3 | — |
| Scenario-20 | MODEL_DECISION_INVALID | Decision/schema failure | 18 | 9 | 9 | 1 | uuid_parsing |
| Scenario-21 | MODEL_DECISION_INVALID | Decision/schema failure | 17 | 11 | 6 | 0 | uuid_parsing |
| Scenario-22 | MODEL_DECISION_INVALID | Decision/schema failure | 16 | 8 | 8 | 0 | uuid_parsing |
| Scenario-23 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 16 | 12 | 2 | 0 | — |
| Scenario-24 | MODEL_DECISION_INVALID | Decision/schema failure | 18 | 11 | 7 | 0 | uuid_parsing |
| Scenario-25 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 13 | 12 | 0 | 2 | — |
| Scenario-29 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 13 | 12 | 0 | 0 | — |
| Scenario-31 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 18 | 12 | 5 | 0 | — |
| Scenario-33 | SUBMIT_DIAGNOSIS | Diagnosis submitted, wrong cause | 12 | 12 | 0 | 1 | — |
| Scenario-34 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 15 | 12 | 1 | 3 | — |
| Scenario-35 | SUBMIT_DIAGNOSIS | Diagnosis submitted, wrong cause | 12 | 12 | 0 | 0 | — |
| Scenario-38 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 15 | 12 | 0 | 2 | — |
| Scenario-80 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 13 | 12 | 0 | 0 | — |
| Scenario-81 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 16 | 12 | 0 | 0 | — |
| Scenario-83 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 19 | 12 | 6 | 0 | — |
| Scenario-91 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 17 | 12 | 0 | 2 | — |
| Scenario-102 | TOOL_CALL_LIMIT | Tool-budget exhaustion | 17 | 12 | 2 | 0 | — |
| Scenario-105 | MODEL_DECISION_INVALID | Decision/schema failure | 16 | 11 | 5 | 2 | uuid_parsing |

## Attribution boundaries

Tool-budget, contract, and evidence-interface defects are system findings. A valid but wrong submitted entity remains a model/causal outcome; no scenario-specific repair is derived from ground truth.
