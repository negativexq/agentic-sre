# When an alert delivery starts a diagnosis

Status: **PROPOSED** (2026-10-02). Owner decision requested; it settles the open question on the `INITIAL`
trigger (roadmap F-list, "revision burst").

## 1. Problem (measured)

Both intake paths (the Connector's alert stream and the local webhook) start a diagnosis for **every delivery**,
whatever it changed, and record it as `INITIAL` unless it continued an episode (`ALERT_REFIRED`). In the ten-hour
product-mode run (`testbed_longrun_run1`):

- 72 incidents produced 300 diagnoses (1 to 6 per incident), all recorded as `INITIAL`, so a revision's trigger
  does not say why it exists.
- After each control-plane restart the alert stream is read again from the Connector's buffer. Ingestion is
  idempotent (no incident is duplicated), but every replayed delivery still started a diagnosis: 14 and 34
  incidents, all of them, were diagnosed again within 40 seconds of the two restarts.
- That burst is where 20 of the 23 transport waits over 10 s came from (snapshot-lock queueing, up to 41 s).

## 2. Rule

Ingestion reports, per incident, what a delivery changed; only a change of the incident starts a diagnosis, with a
trigger that names it.

| What the delivery changed for the incident | Diagnosis | Trigger |
|---|---|---|
| The incident was created | yes | `INITIAL` |
| The incident continued an episode (`Q > 0`, contract §3) | yes | `ALERT_REFIRED` (unchanged) |
| The incident resolved (its last firing occurrence ended, including an occurrence ended by contract §9) | yes | **`RESOLVED`** (new) |
| Nothing: a duplicate, a replay after a restart, a re-delivery of a firing or resolved occurrence, a change of labels or annotations only, an occurrence resolving while another of the incident still fires | no | — |

- **Why a revision at resolution.** It is the diagnosis of the frozen window, the one reports and the late-evidence
  rules rely on (`late-evidence-design.md`); today it exists only because the resolved delivery happens to start a
  diagnosis, and it is labelled `INITIAL`.
- **Unchanged:** `MANUAL` and `EVIDENCE_DEADLINE` revisions, the history of stored revisions (existing `INITIAL`
  rows keep their label), the engine and its version (2.1.0), ingestion's idempotence.
- **Storage:** `RESOLVED` is added to the allowed diagnosis triggers (a migration like `0026`).

## 3. Expected effect (from the same run)

72 creations and 70 resolutions: **142 diagnoses instead of 300** (−53 %), none after a restart, each labelled with
its cause. To be confirmed by re-running the long run after the change.

## 4. Tests

A new occurrence diagnoses once as `INITIAL`; its resolution once as `RESOLVED`; a duplicate or a replayed page
diagnoses nothing; a label-only update diagnoses nothing; an occurrence resolving while another still fires
diagnoses nothing; an incident resolved by §9 gets a `RESOLVED` revision; a refiring keeps `ALERT_REFIRED`. Both
intake paths (stream and webhook) behave the same.

## 5. Decision requested

1. The rule of §2, including the new trigger `RESOLVED`.
2. That a labels- or annotations-only change starts no diagnosis (the alternative is a revision with a trigger such
   as `ALERT_UPDATED`; nothing measured so far needs it).
