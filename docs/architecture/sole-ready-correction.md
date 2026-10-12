# Sole-ready coverage: fail-closed correction

2026-10-12. Authorized scope: correct `_sole_ready`, measure paired effects and
report every lost resolution before applying the production change. Collective
replica coverage, Service/alert meaning, C and HOLDOUT23 are unchanged.

## Contract boundary fixed before the measurement

§28 cannot treat a selected Pod with missing readiness observations as not ready
(§3 I1). Nor can a record only after the execution establish its earlier state.
For each selected Pod other than the witnessed Pod:

- No observations: sole-ready coverage is denied.
- No observation at or before the execution's start: its state at the start is
  unknown; coverage is denied. Future false observations are not backdated.
- The latest observation time at or before the start must unambiguously carry
  `ready=False`. Unknown or conflicting states deny coverage.
- An unknown readiness observation overlapping the execution denies coverage.
- All existing exclusions for a ready competitor remain. In particular a later
  true observation whose `ready_since` reaches into the execution still denies
  coverage. This amendment adds denials only; it cannot create new resolutions.

The existing target-observation requirement remains. This is a correction to
readiness authority in §28, not proof of complete Service membership, not a change
to alarm attribution and not a new collective coverage rule. The helper does not
receive stream-gap metadata; this correction does not claim to close that separate
observation-continuity question.

## Measurement and migration

Use frozen manifest/tape inputs and read-only database sessions. The audit
override withholds only sole-ready coverage where the new preconditions fail.
Record Pod identity, execution interval, readiness records and reason for each
denial. Recalculate the baseline after changed cases to check patch isolation.

Measure all persisted diagnosis runs and flag the widest evidence window per
incident separately, so the prior population of 3,444 windows / 54 resolutions
remains comparable. Count historical recorded results separately from current
engine recalculation. Do not overwrite historical rows or bypass the engine check
in ordinary replay; cross-version recalculation is an explicit audit operation.

Before the production edit, report every lost resolution with frozen run,
incident, alert, leader, denied bridge, unknown other Pod and time interval.
Also report input errors and unexpected root, support, witness or display changes.

After measurement, apply the matching correction, raise the engine minor version
(same `m21.v3` contract family), and keep old-version replay refusals. Tests cover
absent, unknown, future-only, conflicting and later backdated readiness; known
false throughout still permits the existing bridge. Single-Pod and directly
witnessed symptoms retain their existing scope.

## Results

Completed before the production edit: 559 databases (557 with frozen windows,
two without evidence-gathered events), 7,945 runs including intermediate revisions.
There were zero input/recalculation errors and zero lost or new resolutions.

| Measurement | All frozen runs | Widest window per incident |
|---|---:|---:|
| Runs/windows | 7,945 | 3,444 |
| Additional bridge denials | 28 | 23 |
| `RESOLVED` before / after | 101 / 101 | 54 / 54 |
| `AMBIGUOUS` before / after | 7,050 / 7,050 | 2,651 / 2,651 |
| `INSUFFICIENT_EVIDENCE` before / after | 794 / 794 | 739 / 739 |
| Root, support, execution witness, display or digest changes | 0 | 0 |

Every additional denial was `READINESS_AT_EXECUTION_START_UNKNOWN`: records exist
but cannot show the competing Pod's state at the start. All affected runs were
already ambiguous for other reasons. Thus the internal coverage bridge changes
without a change to the serialized decision/digest. Unit counterexamples cover
the completely absent and explicitly unknown cases, not observed in these denials.

**Lost resolution inventory: empty.** The audit keeps `lost_resolutions.json` as
an explicit empty list; there is no lost decision to attribute or grade. Retention
does not certify that the 101 old resolutions are otherwise correct. The change
fixes this particular readiness authority boundary and claims no collective gain.

Local output: `.local/diagnostics/sole-ready/paired/` contains provenance,
`rows.jsonl`, `summary.json`, and `lost_resolutions.json`. Denial entries retain
the witnessed Pod, other Pod, execution interval, and all available readiness
observations. All database access was read-only.

```sh
.venv/bin/python -m scripts.sole_ready_recalculation --output .local/diagnostics/sole-ready/repeat --workers 3
```

The script freezes the old predicate and adds denials with a scoped override.
Its other engine behavior must match the recorded baseline commit for the same
experiment; it is not a generic simulator for arbitrary future engine changes.
After production edit, engine 2.12.0 uses the same preconditions; ordinary replay
rejects 2.11.0 records. The lab process is not restarted by this code change.

Validation: `make check` passes (lint, formatting, type checking; 2,856 tests
passed, 23 optional tests skipped). The applied predicate also matches the
pre-measured policy across 2,353 readiness combinations. Integration tests pin
engine 2.12.0 and reject ordinary replay of both 2.11.0 and older 1.2.2 records.
