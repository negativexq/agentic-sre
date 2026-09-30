# Watch-driven change path (roadmap: event latency, C9 prerequisite)

Status: **APPROVED** (2026-10-01) with the owner's corrections, which `connector-boundary-contract.md` §15 records and which override §3 to §5 below where they differ: an expired version is a `Gap(RESOURCE_VERSION_EXPIRED)` followed by a snapshot, not a silent relist; only a watch `DELETE` on a continuous watch is an observed deletion; the Connector's observation time is recorded now but used causally only with C9.
Companion to `connector-boundary-contract.md` (§10 streams, §14 the latency measurement).

## 1. Problem (measured)

A Kubernetes change reaches the control plane's journal after a median of 21.5 s (p90 27 s; ten rollouts,
`connector-boundary-contract.md` §14) because it waits for two polls in series:

1. the connector lists every watched namespace (11 workload kinds, 6 chaos kinds, Events) every 15 s and appends
   what changed to the change stream;
2. the control plane drains that stream only in its own 15 s loop (`diagnoser.watch`), which also runs the
   journal diff, retention and re-evaluation, and stamps rows with its own clock.

With both intervals at 2 s the median fell to 2.5 s, so nothing else on the path matters. Polling faster is not the
answer: listing every kind of every namespace every two seconds is API load that grows with the cluster.

## 2. Goal

Median change-to-journal delay of **2 s or less**, with **less** API load than today's 15 s polling, and **no change**
to what the stream means: the same items, cursors, epochs, `Gap` reasons and snapshot rules (§10), so every
existing consumer and the in-process/gRPC equivalence stay as they are.

## 3. Connector: list, then watch

Per watched (namespace, kind) scope, and for Events per namespace, the informer pattern:

1. **List** (as today). Its items feed the snapshot or the delta exactly as now; its `resourceVersion` is kept.
2. **Watch** from that `resourceVersion` with bookmarks. Each `ADDED` / `MODIFIED` is emitted at once as an
   `ObjectItem` (or `EventItem`), de-duplicated by the same content digest as today; each `DELETED` as an
   `ObjectDeletedItem`. `observed_at` is the connector's clock when the watch event arrived.
3. **Watch ends** (the server closes watches after some minutes): resume from the last `resourceVersion` or
   bookmark, no relist.
4. **`410 Gone`** (the version expired): relist that scope and emit the difference against what was seen, as a
   delta does today. Nothing is lost, so no `Gap`.
5. **List or watch fails** and cannot be resumed: the scope becomes a failed scope in the next `ListingStatusItem`
   and a `Gap(BACKEND_UNREACHABLE)` is appended, as for a failed poll today.
6. **Reconciliation:** the full poll stays, at a long interval (proposed 10 minutes), as a safety net that the
   existing snapshot and delta code already implements.

Chaos objects and Events follow the same scheme. Secrets stay denied (never listed or watched).

## 4. Control plane: drain on arrival

The control plane stops tying the journal to its 15 s loop. A dedicated thread, like the alert-stream consumer,
reads `read_changes` about once a second; when a page carried anything, it runs the existing journal step (the
mirror-based diff, lifecycle recorder and tombstones) at once. Retention and re-evaluation keep their own slower
cadence. This keeps every journal rule as it is (tombstones only for a completely listed scope, lifecycle,
indexing) and changes only *when* it runs.

## 5. Contract text needed (`connector-boundary-contract.md` §10)

- **A deletion observed by a watch of an established scope is exact.** Today a deletion is inferred only from
  absence in a complete listing; a `DELETED` watch event is an observation of the deletion itself and may produce
  an `ObjectDeletedItem` between listings.
- **`observed_at` of an item is the connector's observation time.** Unchanged in form; with watches it becomes
  close to the change itself.

No new item type and no new `Gap` reason.

## 6. Not in scope

The late-evidence rule of a resolved incident (roadmap C9; it is designed on the latency measured after this);
writing the connector's `observed_at` into the journal instead of the control plane's clock (§8.3 asks whether to
do it now); cluster-wide watches (they need a ClusterRole; the connector's rights are namespaced today).

## 7. Verification

1. **Latency:** the experiment of `connector-boundary-contract.md` §14 (ten rollouts, fresh database) at the default
   settings; accepted when the median is at most 2 s.
2. **API load:** the connector counts its own list and watch calls; compared with today's polling over the same
   ten minutes, accepted when lower.
3. **Equivalence:** every stream test in process and over gRPC, the A3 migration gate (epistemic digests equal),
   and the full suite, unchanged.
4. **Resilience in the lab:** a watch closed by the server resumes without a relist; an expired version relists
   without a `Gap`; a connector restart still declares `CONNECTOR_RESTART` and serves a new snapshot; an API server
   outage produces a failed scope and a `Gap`, and recovery produces a delta.

## 8. Decisions requested

1. The connector design of §3, with the full poll kept as a 10-minute reconciliation.
2. The control-plane drain of §4 (journal step on arrival, retention and re-evaluation on their own cadence).
3. Whether to also write the connector's `observed_at` into the journal now (recommended **later**, with C9, so
   that the latency and the evidence-membership rule change in separate, separately measured steps).
4. The contract text of §5.
5. The acceptance criteria of §7 (median at most 2 s, lower API load, equivalence unchanged).
