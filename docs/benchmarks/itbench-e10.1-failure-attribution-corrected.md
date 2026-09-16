# E10.1 frozen failure attribution

This is post-hoc analysis of immutable E10 artifacts. It made zero provider calls and did not alter E10 evidence.

- scenarios: 35
- provider calls: 0

## Primary bottlenecks

| category | count | fraction |
|---|---:|---:|
| MODEL_SELECTION_MISS | 7 | 0.200 |
| NOT_IN_RECORDED_DISCOVERED_ENTITIES | 23 | 0.657 |
| VERIFICATION_MISS | 5 | 0.143 |

Fields not present in the frozen E10 trace are recorded as `NOT_OBSERVABLE_FROM_FROZEN_ARTIFACT`; no historical state is fabricated.
