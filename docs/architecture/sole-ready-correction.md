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

Pending the paired measurement. No collective coverage gain is claimed.
