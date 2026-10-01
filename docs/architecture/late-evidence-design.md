# Late evidence and evidence continuity (roadmap C9)

Status: **APPROVED** by the owner (2026-10-01, second draft with the amendment of §4.3). Implementation follows in
the order of §8. The first draft mixed two questions; this draft keeps them apart.

## 1. Two questions, not one

- **A. Membership: what may a diagnosis use?** An item the system had observed by the cutoff `T` is evidence the
  diagnosis may use. This is about **positive** evidence: "this happened".
- **B. Coverage: could something have happened that the system did not observe?** This is about every inference
  that rests on **absence**: no effect before `Applied`, no other change, a deletion inferred from a listing, an
  elimination, every symptom covered for `RESOLVED`. Admitting late evidence (A) says nothing about B.

A widens what the engine knows; B bounds what the engine may conclude from what it did not see. A diagnosis needs
both, and neither may stand in for the other.

## 2. Problem (observed)

A resolved incident's window freezes at its resolution, and rows enter by the control plane's **arrival** time.
Arrival time is the boundary on purpose (an Event updated after the incident can keep an old `firstTimestamp`, so a
source time would let hindsight in), but it makes transport delay decide membership: in the `direct-pod-fault`
confirmation run the experiment's `Recovered` event arrived 0.17 s after the resolution, every later diagnosis
treated the execution interval as unclosed, and the fault-execution witness could not form. Measured transport on
the watch path (connector contract §15.5, three hours): source → Connector p99 1.00 s; Connector → journal p99
1.93 s, max 5.64 s. Continuity is measured too: a quiet Event scope loses it about every ten minutes (§15.3).

## 3. A. Membership

1. An Event or object version belongs to a window ending at `T` if the **Connector observed it at or before `T`**
   (`journal_arrivals`, recorded since `8b2d15d`). The Connector's observation is never earlier than the Connector
   actually saw the item, so it admits no hindsight; source times stay out of membership.
2. A row with no recorded Connector observation (polling sources, rows from before `8b2d15d`, every ITBench
   scenario) keeps today's rule: the control plane's arrival at or before `T`.
3. Membership claims nothing about completeness. Whether everything the Connector observed by `T` has arrived is
   B.1 below.

## 4. B. Coverage

For each scope (namespace and kind) the window touches, the diagnosis records whether observation was continuous,
from two independent conditions:

1. **Transport completeness, proven, not assumed.** The change stream is ordered. Once the control plane has read
   the stream past an item whose Connector time is later than `T`, every item the Connector observed by `T` has
   arrived. A change-stream heartbeat (an item carrying only the Connector's time, a few seconds apart) lets a quiet
   stream make that progress. A diagnosis of a resolved incident is built when this holds, or after a timeout
   (`SRE_LATE_EVIDENCE_SECONDS`, proposed 10 s, about five times the measured p99); after a timeout the record says
   **transport completeness not proven**, never that it was complete.
2. **Source continuity.** The control plane persists change-stream gaps with their scope and interval (the Connector
   adds the start: the last instant it observed the scope continuously, which `GapItem` does not carry yet). A scope
   with no gap overlapping the window was observed continuously; otherwise the record names the interval.
3. **The record keeps the two dimensions apart** (owner's amendment): a single value would lose combinations such as
   "the source was continuous but transport was not proven", which recover differently (waiting can prove transport;
   nothing undoes a gap). Per scope:
   - *source continuity*: `CONTINUOUS`, `GAPPED` (with the gap intervals) or `UNKNOWN` (no stream: polling sources,
     rows from before `8b2d15d`, every ITBench scenario);
   - *transport completeness*: `PROVEN` (with the instant the stream was read past `T`), `NOT_PROVEN` (timeout) or
     `NOT_APPLICABLE` (no stream);
   - a *summary* derived, never stored: `COVERED` only when both dimensions are positive, otherwise `NOT_COVERED` with
     the dimension that failed.
   Rules read the dimension they need, never the summary (it is for the console and reports). `UNKNOWN` is not
   continuity: a rule that meets it keeps today's behaviour and records that it did so with coverage unknown, so the
   regression scenarios do not change and no diagnosis claims continuity it does not know. The record is provenance,
   kept with the diagnosis like the alert channel's coverage `W`; it changes no digest field except where §5 makes a
   rule read it.

## 5. B in the engine: where absence is read

Most of the engine already treats absence as neutral (`episode_end.py`: "absence remains neutral"; frontier
answers: "absence and query status cannot answer it"). Absence is read in four places, which would read coverage:

| Place | What it infers from absence | With coverage |
|---|---|---|
| `observed-fault-execution` (`causal_closure.py`) | no effect observation before `Applied` | holds only if the target's Event scope was continuous before `Applied`; otherwise `NOT_ASSESSED` |
| Inferred deletion (journal tombstones, `live.py`) | an object missing from a complete listing is gone | unchanged (it already needs a complete scope); its time stays unknown |
| Ended-episode and temporal elimination (`resolution.py`) | an episode ended because its object is gone / nothing later happened | eliminates only on continuous scopes; otherwise `ELIMINATION_NOT_HOLDING` |
| `RESOLVED` (no unresolved admitted rival) | no rival change was missed | requires the change scopes of the window to be continuous |

Each of these is a rule change with its own measurement (the 35 regression scenarios have no Connector rows, so
they keep today's behaviour; the testbed runs supply the cases).

## 6. Not in scope

Source times in membership; adding items to stored revisions; alert timing (Alertmanager's own times, the episode
contract). Open incidents are in scope for coverage only: their window ends at the capture time, and their diagnosis
waits for the same transport proof as a resolution (owner's decision 2026-10-01; about one heartbeat, 2 s), so an open
diagnosis does not record `NOT_PROVEN` merely because its cutoff is "now".

## 7. Verification

1. **A, unit:** an item the Connector observed before `T` is a member however late it arrived; one observed after `T`
   is not, however early its source time; a row without a Connector observation keeps the arrival rule.
2. **B, unit:** the stream read past `T` proves transport completeness; a timeout records it as not proven; a gap
   overlapping the window is recorded with its interval; a scope without a gap is continuous.
3. **Regression:** the 35 ITBench scenarios and the replay digests of recorded runs unchanged.
4. **Live:** the `direct-pod-fault` confirmation case (the `Recovered` race of §2) re-run a few times: the closing
   event is a member whenever the Connector observed it before the resolution, no member was observed after it, and
   the coverage record is `CONTINUOUS` for the target's scopes or names the gap that was there.

## 8. Decisions (approved 2026-10-01)

1. **A** as §3: membership by the Connector's observation time, today's rule for rows without one.
2. **B** as §4: transport completeness proven from the stream with a change-stream heartbeat and a 10 s timeout;
   gaps persisted with their start; a per-scope coverage record kept with the diagnosis.
3. **Order:** A and B (recording) first, as one step; then the four engine readings of §5, each proposed and measured
   on its own, starting with `observed-fault-execution`, which is where the observed failure was.

## 9. Live check of the recording (2026-10-01, three unscored `direct-pod-fault` runs)

Commits `75d6f57`, `bd25cc6`, `8e5b067`, `5cbbb01`; connector image with heartbeats; a fresh control plane and
database per run.

- **Valid** 3/3, cause and instance named 3/3, no false strong authority, no false `RESOLVED`. The execution witness
  formed in run 3 (the target pod failed a probe inside the interval; the coverage record is not read by the rule).
- **Membership.** The closing `Recovered` reached the journal 0.06 s, 0.25 s and 0.58 s after the Connector
  observed it, about 40 s before each resolution: on the watch path the race of §2 did not recur. No diagnosis
  admitted an item observed after its cutoff.
- **Transport completeness** `PROVEN` in all 16 diagnoses (open and resolved), none timed out. Wait from the cutoff
  to the proof: median 1.8 s, max 7.0 s (run 3, two incidents diagnosed at once while the experiment recovered). The
  7.0 s is within the 10 s timeout but closer than the 2 s heartbeat explains: each wait iteration runs a full
  journal step under the snapshot lock. Open.
- **Source continuity** `UNKNOWN` in every diagnosis: the testbed starts the control plane per run, so each window
  begins before the stream was followed. This is the rule of §4.3 working as written, but the live check therefore
  did not exercise `CONTINUOUS` or `GAPPED`; no gap occurred in these short runs. A rule that reads continuity
  should read it over its own interval (for `observed-fault-execution`: before `Applied`) from the recorded
  `stream_followed_since` and gaps, not the whole window's verdict.
