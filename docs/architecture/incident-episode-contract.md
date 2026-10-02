# Alert episodes and incidents (`incident.episode.v1`)

Status: **APPROVED** by the owner (2026-09-30) with the amendments of §8. **Implemented** (tests first): `IncidentManager(quiet=...)`, `ingest_occurrence`, the `ALERT_REFIRED` event and revision trigger (migration `0026`), `SRE_ALERT_QUIET_SECONDS` read by the webhook and the stream intake. The default is `0`, so no deployment changes behaviour.
Amends the occurrence rule of `docs/architecture.md` ("Alertmanager occurrence identity").

## 1. Problem

One alert fingerprint that flaps opens a new incident every time it fires again: the occurrence identity is
`(fingerprint, starts_at)` and every new start time is a new incident. In the testbed, one fault opened
3 to 7 incidents; part of that is flapping (slice 3 repeat 1: `PaymentRequestLatencyHigh`,
`OrderDependencyLatencyHigh` and `HighRequestLatency` resolved and fired again). The operator sees the same
problem several times and the engine diagnoses it several times.

This contract covers **only one fingerprint firing again**. Folding *different* alerts into one incident is a
correlation problem and is out of scope (§7).

## 2. Terms

- **Occurrence:** one Alertmanager alert, identified by `(fingerprint, starts_at)`; unchanged.
- **Episode:** one incident. It holds one or more occurrences of the same fingerprint.
- **Gap:** for a new occurrence `O2`, the time from the end of the fingerprint's latest earlier occurrence `O1`
  (`O1.ends_at`) to `O2.starts_at`. Both are Alertmanager's own times, never arrival times, so the rule does not
  depend on how late a delivery reaches the control plane (roadmap C9, connector §14).
- **Quiet interval `Q`:** the shortest gap that separates two episodes.

## 3. Rule

The mechanism is **off when `Q = 0`**, and then ingestion behaves exactly as before this contract: every new
occurrence opens a new incident, including one that arrives while an earlier occurrence still fires and one whose
start precedes the earlier occurrence's end. With `Q > 0`, when an occurrence `O2` of fingerprint `F` arrives
firing and is not already known:

1. If `F` has an earlier occurrence `O1` that is **still firing**, `O2` continues `O1`'s episode.
2. Else, if `F`'s latest earlier occurrence `O1` resolved and `gap < Q` (a negative gap, `O2` starting before
   `O1` ended, is a continuation too), `O2` **continues** `O1`'s episode: the
   incident goes from `RESOLVED` back to `OPEN`, and its timeline records `ALERT_REFIRED` with the gap.
3. Else (no earlier occurrence, or `gap >= Q`), `O2` opens a **new** incident, as today.

Unchanged: repeated delivery of a known occurrence is idempotent, a late firing of an occurrence already
resolved does not reopen it, and the uniqueness constraint on `(fingerprint, starts_at)` stays.

## 4. What a continuation does

- **History is never rewritten.** The incident keeps its identity and its timeline keeps every transition: the
  earlier `ALERT_RESOLVED`, then `ALERT_REFIRED` (with the gap and the new occurrence), then the incident open again.
  The timeline reads `OPEN → RESOLVED → (quiet gap) → ALERT_REFIRED → OPEN`, never as if it had not resolved.
- **Earlier diagnoses and reports stay as they were.** Revisions taken while the incident was resolved keep their
  frozen windows; reports stay pinned to their `diagnosis_run_id`.
- **An episode resolves when none of its occurrences still fires** (with rule 1 an incident can hold two
  firing occurrences at once).
- **Diagnosis.** The incident gets a new revision with a new trigger `ALERT_REFIRED`. Its causal window is that
  of an open incident again (it grows with the evidence) until the episode resolves.
- **Resolution.** The episode resolves when its latest occurrence resolves; the frozen window of a resolved
  incident is taken at that later resolution.
- **Console.** A continuation is not a new incident, so no "new incident" notification; the incident's status
  and timeline show it (`docs/ui/product-contract.md`, Notifications, unchanged).

## 5. Choosing `Q`

`Q` is configuration (`SRE_ALERT_QUIET_SECONDS`). **The product default is `0` (the mechanism off, today's
behaviour)** until soak data has been reviewed and the owner has frozen a default. The **testbed uses a
provisional `Q = 30 s` only in the soak run**, which exists to choose the value; the scenario slices run with the
product default so that their results stay comparable, and every suite records its `Q` with its frozen settings. Measured in the lab (22 run databases, 32 re-firings): gaps of
6 s and about 20 s were one episode; every gap of 90 s or more was a different cause; none fell between. That
bounds `Q` in this lab to between 20 s and 90 s but does not choose it: the sample is small, the lab's faults are
removed after about 100 s and its rules' `for:` shapes the gaps. The default is therefore **provisional** and
is revisited with soak data (§8.2). A source-side alternative exists (Prometheus `keep_firing_for` suppresses
flaps in the rule itself); the product cannot rely on it because customers own their rules.

## 6. Tests to add

A re-firing within `Q` continues the episode and reopens it; at exactly `Q` and beyond, a new incident; a
re-firing while the earlier occurrence still fires continues it; a different fingerprint never continues another's
episode; the timeline records `ALERT_REFIRED` with the gap; a continuation triggers one `ALERT_REFIRED`
revision. The existing `test_resolved_fingerprint_creates_a_new_incident_episode` (a 10-minute gap) stays as
it is and must still pass.

## 7. Out of scope

Grouping different alerts into one incident (to be decided after the `competing-causes` family is measured);
suppressing the incident for an occurrence that first arrives already resolved (the stale resend of a new
database, `testbed-scenarios-design.md`); any change to how the engine selects a cause.

## 8. Decisions (2026-09-30)

Approved: the rule of §3; the effects of §4 with `ALERT_REFIRED`; `Q` measured from Alertmanager's times.
Amended by the owner: 30 s is not a product default; the product default is `Q = 0` until soak data is reviewed,
and 30 s is a provisional testbed value for the soak only. Added on review: `Q = 0` turns the whole mechanism off
(rule 1 and negative gaps included); history, earlier diagnoses and reports are never rewritten.

The original requests, for the record:

1. The rule of §3 and its effects in §4, including the new timeline event and revision trigger `ALERT_REFIRED`.
2. The provisional default of `Q`: **30 s** is recommended (above the largest continuation observed, 20 s, and
   well below the smallest separate episode, 90 s), to be revisited with soak data. The alternative is to
   ship with `Q = 0` (today's behaviour) until the soak exists.
3. `Q` measured from `O1.ends_at` to `O2.starts_at` (Alertmanager's times), not from arrival times.

## 9. Amendment: a replaced occurrence ends (APPROVED and implemented, 2026-10-02)

**Observed.** In the ten-hour product-mode run, two incidents (`KafkaConsumerLag`, `OrderWorkerLagHigh`) stayed
`OPEN` for four hours after their alerts had ended. Each fingerprint resolved and fired again within about a
minute; Alertmanager reported the new occurrence (new `startsAt`) and never a resolution of the old one, and the
Connector's poll, keyed by fingerprint alone, saw no change. The Connector now ends a replaced occurrence itself
(`3732b48`); the rule below makes ingestion agree, so a webhook-only deployment and a missed poll behave the same.

**Rule.** Alertmanager holds at most one alert per fingerprint. When a firing occurrence `O2` of fingerprint `F`
arrives and an earlier occurrence `O1` of `F` (`O1.starts_at < O2.starts_at`) is still `FIRING`:

1. `O1` ends: its status becomes `RESOLVED` with `ends_at = O2.starts_at`. That instant is a bound (`O1` ended no
   later than `O2` began), not an observation, and the timeline says so: the incident's `ALERT_RESOLVED` event
   carries `{"inferred": "superseded", "by_starts_at": O2.starts_at}`. No new event type.
2. `O1`'s incident then follows the existing rule: it resolves when none of its occurrences still fires.
3. `O2` is ingested exactly as today: with `Q = 0` it opens a new incident; with `Q > 0` rule 1 of §3 continues
   `O1`'s episode, which stays open through `O2`.

**What it amends.** §3 says that with `Q = 0` a new occurrence "that arrives while an earlier occurrence still
fires" opens a new incident. That stays true for `O2`; what changes is that `O1` no longer stays firing beside it.
History is not rewritten: `O1`'s rows and earlier diagnoses remain; only its end is added.

**Tests.** A replaced occurrence ends at the new start and its incident resolves (`Q = 0`); with `Q > 0` the
episode continues and stays open; a late duplicate of `O1` does not reopen it; a different fingerprint is unaffected.
