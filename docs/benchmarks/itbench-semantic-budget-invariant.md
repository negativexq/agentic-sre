# ITBench semantic budget invariant

Offline-only proof that capability availability intersects, rather than replaces, the CaseState semantic-action budget.

| Case | Used/limit | Resolver operations | Effective operations | Valid actions | Result |
|---|---:|---|---|---|---|
| BELOW_LIMIT | 2/3 | ENTITY_CONTEXT | ENTITY_CONTEXT | INVESTIGATE, REVISE, STOP | PASS |
| AT_LIMIT | 3/3 | ENTITY_CONTEXT, EVENT_ANALYSIS, SPEC_ANALYSIS | — | REVISE, STOP | PASS |
| OVER_LIMIT | 4/3 | ENTITY_CONTEXT, EVENT_ANALYSIS, SPEC_ANALYSIS | — | REVISE, STOP | PASS |
| CAPABILITY_INTERSECTION | 0/3 | ENTITY_CONTEXT, SPEC_ANALYSIS | SPEC_ANALYSIS | INVESTIGATE, REVISE, STOP | PASS |
| TARGET_SCOPED_DUPLICATE | 1/3 | ENTITY_CONTEXT | ENTITY_CONTEXT | INVESTIGATE, REVISE, STOP | PASS |
| SUBMIT_AT_LIMIT | 3/3 | ENTITY_CONTEXT | — | REVISE, SUBMIT, STOP | PASS |
| NO_EVIDENCE_AT_LIMIT | 3/3 | ENTITY_CONTEXT | — | REVISE, STOP | PASS |

The authoritative budget is `CaseState.semantic_actions_used`. At or over the limit, `INVESTIGATE` is absent while valid `SUBMIT`, `REVISE`, and `STOP` branches remain governed by their own preconditions.

Live/model calls: 0. Judge calls: 0.
