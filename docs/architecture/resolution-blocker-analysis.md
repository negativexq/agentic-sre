# Resolution Blocker Analysis v1

2026-10-14. Implemented as offline analysis on `fix/dependency-service-attribution`.
This reads recorded decisions; it grants no RCA, exclusion, planner or frontier authority.

## Inputs and boundary

`packages/evals/resolution_blockers.py` accepts raw diagnosis dictionaries. Reading raw fields is deliberate:
parsing an old document through model defaults can turn an absent inventory into an apparently empty one.
The four M21 v3 conditions are reported separately:

- `UNIQUE_STRONG_CAUSE`: recorded `plausible_hypotheses` has one member and equals
  `mechanism_verified_hypotheses`.
- `WITNESS_SCOPE_COVERED`: declared symptom entities are explicitly covered by fired
  `OBSERVED_MECHANISM_CAUSE` witnesses, or a recorded, internally consistent M21 `RESOLVED`
  decision certifies scope. Otherwise `UNASSESSED`: this report does not reconstruct §27/§28
  ownership/sole-ready bridges or call `_covered_symptoms`. Missing direct coverage is not a negative verdict.
- `NO_UNRESOLVED_RIVALS`: the recorded unresolved claim inventory is empty.
- `MATERIAL_FRONTIER_CLOSED`: the recorded limiting `material_frontier_ids` is empty.
  An unrelated `OPEN` answer and an answered dependency's additional contribution do not become blockers.

Statuses are `SATISFIED`, `UNSATISFIED`, `UNASSESSED` and `NOT_RECORDED`. Unsupported semantics are
unassessed. Missing fields are not recorded, never treated as empty inventories. A report never produces
or changes a resolution, even if all four reported conditions are satisfied.

Witness scope is **not measurement accounting**. The latter is explicitly not assessed here.
The report counts confirmed unmet conditions and limiting material questions separately; unknown conditions
remain visible. It does not claim independence between blockers or estimate resolution probability.

## Evidence deficits

Recorded reasons remain verbatim alongside descriptive codes and evidence/gap references. Read failure,
missing capture, unsupported expression, unknown timing, insufficient observations, absent initiating
support and current tool limitations are distinct. Information gaps remain the engine's own questions;
the report does not invent a query, infer a missing causal rule from an unresolved claim, or assume a
queryable gap can be conclusively answered.

A missing final scope verdict is a reporting limitation, not a new resolution blocker. Condition combinations
in summaries count **confirmed** failures; per-condition status counts and unassessed revision counts must
be shown beside them. `ONE_MATERIAL_QUESTION_REMAINING` requires one limiting question, only the frontier
condition unmet, and no unassessed condition.

## CLI

```sh
python -m scripts.resolution_blocker_analysis diagnoses.json --output blockers.json
python -m scripts.resolution_blocker_analysis revisions.jsonl --output blockers.json
python -m scripts.resolution_blocker_analysis \
  --database testbed_holdout23_negative_b_negative_image_decoy_0 \
  --output .local/diagnostics/resolution-blockers/negative-b-0.json
```

JSON supports a diagnosis, a list of diagnoses, or exported rows containing `document`. JSONL supports
one such object per line. Wrapper metadata (`run_id`, `revision_number`, `engine_version`,
`assessment_kind`) is preserved. Counterfactual documents use `OFFLINE_RECALCULATION`, never
`RECORDED_DIAGNOSIS`.

Database mode uses the existing local testbed read-only connection helper and reads **all stored diagnosis
revisions**, not only each incident's final revision. It invokes no providers, investigation actions or diagnosis
rules. Output is a separate JSON artifact with revision reports, condition distributions, evidence deficit
counts and the conservative single-material-question list. Output may not overwrite an input file.

## Validation

Tests exercise missing and legacy fields, positive direct witness scope, unknown indirect coverage,
recorded resolution authority, multiple questions versus one failed condition, nonmaterial open questions,
additional-contribution uncertainty, concrete recorded gaps and immutable CLI inputs. Neither engine inputs
nor decision fields/digests acquire this output. No engine version bump is needed for this analysis layer.

HOLDOUT23 is development data for this work. Reading its recorded revisions does not re-score it or change
its frozen protocol, inputs or stored diagnoses. New resolution-authorizing rules require separate validation.
