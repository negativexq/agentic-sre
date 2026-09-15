# ITBench semantic operation coverage v2 (offline)

Capability availability is observable-snapshot availability, not RCA quality.

| Operation | Scenarios | Available/provider | Useful | Unavailable | Inconclusive | Errors | Median | P95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| INCIDENT_OVERVIEW | 35 | 35 | 25 | 0 | 0 | 0 | 2915 | 5439 |
| ALERT_ANALYSIS | 35 | 35 | 35 | 0 | 0 | 0 | 4543 | 5172 |
| TOPOLOGY_ANALYSIS | 35 | 35 | 35 | 0 | 0 | 0 | 1233 | 1233 |
| RECENT_CHANGE_ANALYSIS | 35 | 0 | 0 | 35 | 0 | 0 | 1323 | 2625 |
| ENTITY_CONTEXT | 35 | 35 | 35 | 0 | 0 | 0 | 1942 | 1984 |
| EVENT_ANALYSIS | 35 | 35 | 35 | 0 | 0 | 0 | 1036 | 1037 |
| METRIC_ANOMALIES | 35 | 35 | 1 | 0 | 0 | 0 | 412 | 413 |
| TRACE_ERROR_TREE | 35 | 1 | 1 | 34 | 0 | 0 | 63 | 63 |
| SPEC_ANALYSIS | 35 | 35 | 35 | 0 | 0 | 0 | 1932 | 2311 |
| COMPARE_REPLICAS | 35 | 0 | 0 | 35 | 0 | 0 | 176 | 176 |
| VERIFY_TEMPORAL_ALIGNMENT | 35 | 0 | 0 | 0 | 35 | 0 | 266 | 326 |
