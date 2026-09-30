# Alert episodes and incidents (`incident.episode.v1`)

Status: **PROPOSED** (2026-09-30), awaiting the owner's decisions in §8. Nothing here is implemented.
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

When an occurrence `O2` of fingerprint `F` arrives firing and is not already known:

1. If `F` has an earlier occurrence `O1` that is **still firing**, `O2` continues `O1`'s episode.
2. Else, if `F`'s latest earlier occurrence `O1` resolved and `gap < Q`, `O2` **continues** `O1`'s episode: the
   incident goes from `RESOLVED` back to `OPEN`, and its timeline records `ALERT_REFIRED` with the gap.
3. Else (no earlier occurrence, or `gap >= Q`), `O2` opens a **new** incident, as today.

Unchanged: repeated delivery of a known occurrence is idempotent, a late firing of an occurrence already
resolved does not reopen it, and the uniqueness constraint on `(fingerprint, starts_at)` stays.

## 4. What a continuation does

- **Diagnosis.** The incident gets a new revision with a new trigger `ALERT_REFIRED`. Its causal window is that
  of an open incident again (it grows with the evidence) until the episode resolves.
- **Resolution.** The episode resolves when its latest occurrence resolves; the frozen window of a resolved
  incident is taken at that later resolution.
- **Console.** A continuation is not a new incident, so no "new incident" notification; the incident's status
  and timeline show it (`docs/ui/product-contract.md`, Notifications, unchanged).

## 5. Choosing `Q`

`Q` is configuration (`SRE_ALERT_QUIET_SECONDS`). Measured in the lab (22 run databases, 32 re-firings): gaps of
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

## 8. Decisions requested

1. The rule of §3 and its effects in §4, including the new timeline event and revision trigger `ALERT_REFIRED`.
2. The provisional default of `Q`: **30 s** is recommended (above the largest continuation observed, 20 s, and
   well below the smallest separate episode, 90 s), to be revisited with soak data. The alternative is to
   ship with `Q = 0` (today's behaviour) until the soak exists.
3. `Q` measured from `O1.ends_at` to `O2.starts_at` (Alertmanager's times), not from arrival times.
