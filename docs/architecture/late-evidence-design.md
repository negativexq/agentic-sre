# Evidence that arrives after a resolution (roadmap C9)

Status: **PROPOSED** (2026-10-01), awaiting the owner's decisions in §7. Nothing here is implemented.

## 1. Problem (observed)

A resolved incident's evidence window freezes at its resolution, and evidence enters a diagnosis by the control
plane's **arrival** time (`observed_at <= window_end`, `EventRepository.history_versions` and the object journal).
Arrival time is the epistemic boundary on purpose: an Event can be updated after the incident while keeping an old
`firstTimestamp` (a re-created aggregate carries its cached first time), so a source time would let hindsight in.

But transport delay now decides membership. In the confirmation run of `direct-pod-fault` the experiment's
`Recovered` event happened at 15:02:59 and arrived 0.17 s after the incident resolved, so every later diagnosis
treated the execution interval as unclosed and the fault-execution witness could not form. With the watch path the
delay is short and measured (connector contract §15.5, three hours): source → Connector p50 0.48 s, p99 1.00 s;
Connector → journal p50 0.73 s, p99 1.93 s, max 5.64 s.

## 2. Principle

Keep "what the system had observed at the cutoff", but take the observation where it happened: at the **Connector**.
An item the Connector observed at or before the cutoff was known to the system then; the time it spent crossing to the
control plane is transport, not knowledge. The Connector's observation time is recorded for every streamed row since
`8b2d15d` (`journal_arrivals`), and it is never earlier than the Connector actually saw the item, so it admits no
hindsight; source times stay out of membership.

## 3. Rule

1. **Membership.** An Event or object version belongs to a window ending at `T` if the Connector observed it at or
   before `T`; a row with no recorded Connector observation (polling sources, rows from before `8b2d15d`) keeps
   today's rule, the control plane's arrival at or before `T`.
2. **Settling.** A diagnosis of a resolved incident is not built until `T + L`, so that items the Connector observed
   before `T` have arrived. `L` is configuration (`SRE_LATE_EVIDENCE_SECONDS`), proposed **10 s**: about five times the
   Connector → journal p99 measured over three hours, and above its single observed maximum (5.64 s).
3. **Late beyond `L`.** An item the Connector observed before `T` but that arrived after `T + L` is not added
   afterwards to a stored diagnosis (stored revisions never change); a later revision includes it by rule 1. The
   diagnosis records how many such items it could not have seen, so the case is visible rather than silent.
4. **Continuity at the cutoff.** If the change stream had a `Gap` for a scope that overlaps the window, the diagnosis
   states that continuity for that scope (namespace and kind) was not guaranteed in that interval, as alert-channel
   coverage already does for alerts. This needs the control plane to persist change-stream gaps (it does not today)
   and the gap to carry its start (§15.5: not recorded yet).

## 4. What it changes and what it does not

- Changes: which rows a resolved incident's diagnosis may include (rule 1), when that diagnosis is built (rule 2), and
  two new pieces of diagnosis provenance (rules 3 and 4).
- Unchanged: open incidents (their window is the capture time, as now); replay (a run's manifest lists its members, so
  a recorded run replays to the same digest); the epistemic digest's fields; every causal rule.
- Rows from before `8b2d15d`, and every ITBench scenario, have no Connector observation and therefore keep today's
  rule exactly; the 35 regression scenarios cannot change.

## 5. Not in scope

Using source times for membership; adding late items to stored revisions; open incidents; alert-occurrence timing
(alerts already carry Alertmanager's own times, the episode contract).

## 6. Verification

1. **Unit:** an item observed by the Connector before `T` and arriving within `L` is a member; one observed after `T`
   is not, however early its source time; a row with no Connector observation keeps the arrival rule; a scope gap
   overlapping the window appears in the diagnosis.
2. **Regression:** the 35 ITBench scenarios and the replay digests of recorded runs unchanged.
3. **Live:** re-run the `direct-pod-fault` confirmation case (the `Recovered` race of §1) and a short suite of it;
   accepted when the closing event is a member in every run where the Connector observed it before the resolution, and
   when no run gains a member observed after it.

## 7. Decisions requested

1. Membership by the Connector's observation time, with today's rule kept for rows without one (§3.1).
2. Settling before a resolved incident's diagnosis, `L = 10 s` configurable (§3.2).
3. Late items recorded as a count, never added to stored revisions (§3.3).
4. Persisting change-stream gaps with their start, and reporting overlapping gaps in the diagnosis (§3.4), as a
   second step after 1 to 3.
