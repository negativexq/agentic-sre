# Diagnosis timeline (v2a)

The control plane records a small, honest timeline of every diagnosis run so the
incident UI can show what happened between an alert firing and a root cause being
stored, and how long each stage took. This is observability added to the existing
diagnosis pipeline — it does not change the RCA engine or its output.

## What is recorded

Each call to `DiagnosisService.run()` is tagged with its own `run_id` and emits
four immutable `IncidentEvent`s, in order:

| Event | When | Payload |
|---|---|---|
| `DIAGNOSIS_STARTED` | run begins | `run_id`, `alerts` |
| `EVIDENCE_GATHERED` | observations collected | `run_id`, `objects`, `journal`, `events`, `logs` |
| `HYPOTHESIS_CREATED` | RCA engine returned | `run_id`, `leading_actor`, `reads`, `evidence` |
| `DIAGNOSIS_COMPLETED` | diagnosis stored | `run_id`, `root_cause`, `resolution`, `confidence`, `model_calls` |

The counts are the real source counts and the real diagnosis output; nothing is
estimated. `DIAGNOSIS_COMPLETED` is used rather than a `ROOT_CAUSE_IDENTIFIED`
event because not every run ends with a root cause — an `INSUFFICIENT_EVIDENCE`
run completes with `root_cause = null`, and the timeline records that honestly.

## Why `run_id` instead of suppressing re-diagnosis

The control plane re-diagnoses an open incident on its watch cycle. Rather than
suppressing events on later runs (which would hide real work), each run carries a
distinct `run_id`. The timeline keeps every run; the incident UI shows the phases
of the most recent one, selected by its latest event timestamp. A re-diagnosis is
therefore visible as a new run, never mistaken for a duplicate.

## Where it shows

- `GET /api/v1/incidents/{id}/events` returns the full ordered timeline.
- The incident page (`/incidents/{id}`) renders the latest run's phases inside
  the existing **Lifecycle** section, as `alert fired → incident opened →
  diagnosis started → evidence gathered → RCA engine completed → diagnosis
  stored`, each with a `T+` offset from the start of the run.

Recording is best effort: a failure to write a timeline event is logged and
swallowed, so observability can never fail or slow a diagnosis.

## Verified behaviour

`tests/integration/test_diagnosis_timeline.py` asserts: exactly one
`DIAGNOSIS_STARTED` and one `DIAGNOSIS_COMPLETED` per run; monotonic timestamps;
`EVIDENCE_GATHERED` counts equal to the real source counts; a re-diagnosis
appends a new `run_id`; the events API returns the timeline in order; and the
incident page renders the phase timeline. The existing RCA-engine and benchmark
tests are unchanged and still pass, confirming the engine's output did not move.

## Not in scope (deferred to v2b)

Per-read timing inside the RCA engine (a timestamp on each `InvestigationStep`)
is deliberately out of scope. It would enlarge the deterministic core's
serialization, hashing, and test surface, so it is a separate decision worth an
ADR when taken. v2a measures only the outer pipeline boundaries, which the
control plane already owns.
