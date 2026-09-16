# ITBench semantic operation coverage v3 (offline)

Availability is derived from observable snapshot capability; it is not RCA quality.

| Operation | Scope | Scenarios | Available/provider | Useful | Unavailable | Inconclusive | Errors | Median chars | P95 chars |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| INCIDENT_OVERVIEW | GLOBAL | 35 | 35 | 25 | 0 | 0 | 0 | 2915 | 5439 |
| ALERT_ANALYSIS | GLOBAL | 35 | 35 | 35 | 0 | 0 | 0 | 4543 | 5172 |
| TOPOLOGY_ANALYSIS | GLOBAL | 35 | 35 | 35 | 0 | 0 | 0 | 1233 | 1233 |
| RECENT_CHANGE_ANALYSIS | GLOBAL | 35 | 0 | 0 | 35 | 0 | 0 | 1323 | 2625 |
| ENTITY_CONTEXT | TARGET | 35 | 35 | 35 | 0 | 0 | 0 | 1942 | 1984 |
| EVENT_ANALYSIS | TARGET | 35 | 35 | 35 | 0 | 0 | 0 | 1036 | 1037 |
| METRIC_ANOMALIES | TARGET | 35 | 1 | 1 | 34 | 0 | 0 | 412 | 413 |
| TRACE_ERROR_TREE | TARGET | 35 | 1 | 1 | 34 | 0 | 0 | 63 | 63 |
| SPEC_ANALYSIS | TARGET | 35 | 35 | 35 | 0 | 0 | 0 | 1932 | 2311 |
| COMPARE_REPLICAS | TARGET | 35 | 0 | 0 | 35 | 0 | 0 | 176 | 176 |
| VERIFY_TEMPORAL_ALIGNMENT | TARGET | 35 | 0 | 0 | 0 | 35 | 0 | 266 | 326 |
