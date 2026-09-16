# ITBench initial packet / operation overlap (offline)

This report compares the durable 35-snapshot context qualification with the
durable semantic-operation coverage matrix. It uses no ground truth and no
model calls. The overlap is a contract/section duplication measurement, not
an RCA score.

| Operation | Automatic packet median chars | Operation median chars | Scenarios with duplicated purpose |
|---|---:|---:|---:|
| `ALERT_ANALYSIS` | 2,881 | 4,543 | 35/35 |
| `TOPOLOGY_ANALYSIS` | 1,422 | 1,233 | 35/35 |
| `INCIDENT_OVERVIEW` | 3,089 (incident + candidates) | 2,915 | 35/35 |

The automatic packet is harness-owned and already supplies the common alert,
topology, incident and candidate facts. The final interface therefore removes
these three rediscovery operations from the initial provider surface. The
underlying backend helpers remain available to internal qualification code.

The JSON artifact records the per-snapshot sizes and the source artifacts used
for this offline comparison.
