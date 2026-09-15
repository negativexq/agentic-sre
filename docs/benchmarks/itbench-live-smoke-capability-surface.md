# Future Luna smoke capability surface (offline)

This is a capability report, not an RCA score.

| Operation | Scope | Target | OBSERVE available | VERIFY available | Provider | Runtime | Context |
|---|---|---:|---:|---:|---|---|---|
| INCIDENT_OVERVIEW | GLOBAL | False | 35 | 35 | PASS | PASS | PASS |
| ALERT_ANALYSIS | GLOBAL | False | 35 | 35 | PASS | PASS | PASS |
| TOPOLOGY_ANALYSIS | GLOBAL | False | 35 | 35 | PASS | PASS | PASS |
| RECENT_CHANGE_ANALYSIS | GLOBAL | False | 0 | 0 | PASS | PASS | PASS |
| ENTITY_CONTEXT | TARGET | True | 0 | 35 | PASS | PASS | PASS |
| EVENT_ANALYSIS | GLOBAL | False | 35 | 35 | PASS | PASS | PASS |
| METRIC_ANOMALIES | TARGET | True | 0 | 35 | PASS | PASS | PASS |
| TRACE_ERROR_TREE | TARGET | True | 0 | 1 | PASS | PASS | PASS |
| SPEC_ANALYSIS | TARGET | True | 0 | 35 | PASS | PASS | PASS |
| COMPARE_REPLICAS | TARGET | True | 0 | 0 | PASS | PASS | PASS |
| VERIFY_TEMPORAL_ALIGNMENT | TARGET | True | 0 | 0 | PASS | PASS | PASS |

Disabled by default on the frozen snapshots: RECENT_CHANGE_ANALYSIS, COMPARE_REPLICAS, VERIFY_TEMPORAL_ALIGNMENT
