# Connector boundary contract (`connector.v1`)

Status: **PROPOSED** (2026-09-30). Nothing here is implemented; owner approval is required before
any code. Amendments are made here first (same rule as the M21 contracts).

## 1. Invariant

The Control Plane holds no credential for, and makes no direct connection to, any customer
resource. Every observation and every read of Kubernetes, Prometheus, Loki, Tempo and
Alertmanager reaches it only through the Connector's **typed, bounded, audited** operations
below. The Connector only ever dials out.

*Customer resource* means anything in the customer's environment. The Control Plane's own
database and its model provider are not customer resources.

## 2. Where the boundary already runs

The code already has the seams; this contract names them instead of inventing new ones.

- `ClusterReader` / `LogReader` (`packages/rca/live.py`) read Kubernetes and Loki; `ProviderBoundary`
  (`provider_adapter.py`) reads Prometheus and Tempo; `ChangeWatcher` polls the cluster into the
  Control Plane's journal.
- `InvestigationQueryWire` is already a strict semantic query, "not a backend query language".
- `ProviderReadFailure` already separates a failed read from an empty one.
- `ReadTape` records every source and backend call by name; it is the golden recording of this
  boundary (`_SOURCE_METHODS`, `query_*`).

## 3. Operation catalog

**Ownership rule.** State derived from persisted observations stays Control Plane-owned: object
history (journal and snapshot cycles), pod status (lifecycle ledger), alert episodes and alert
coverage `W`, the epistemic digest. The Connector supplies *observations*, never history. This
keeps replay and the digest independent of the connector's uptime.

| Op | Kind | Backend | Returns |
|---|---|---|---|
| `capabilities` | request | Connector | version, wire schema, per-capability availability and coverage (today's `supports`) |
| `preflight` | request | all | per-backend reachability and permission result; no data |
| `list_objects(scope)` | request | Kubernetes | `ObjectListing` (objects plus a `ListingFailure` per incomplete scope) |
| `list_events(scope)` | request | Kubernetes | events |
| `query_logs(target, q)` | request | Loki | log records or `ReadFailure` |
| `query_resource_pressure(target, q)` | request | Prometheus | records or `ReadFailure` |
| `query_traffic(target, q)` | request | Prometheus | records or `ReadFailure` (live reader does not exist yet) |
| `query_traces(target, q)` | request | Tempo | spans or `ReadFailure` (live reader does not exist yet) |
| `list_alerts` | request | Alertmanager | active alerts (the coverage poll) |
| `alerts` | stream | Alertmanager | firing/resolved occurrences |
| `changes` | stream | Kubernetes | object versions and events since a cursor (replaces the `ChangeWatcher` poll loop's read side) |

`q` is `InvestigationQueryWire` (`limit` at most 64, bounded window). Every request is
size-, time- and rate-limited **by the Connector**; the Control Plane cannot raise a limit.

**Deliberately absent:** raw PromQL/LogQL/TraceQL or any query-string pass-through; any write
or mutating operation (apply, delete, patch, exec, scale); arbitrary URL fetch; reading `Secret`
objects; unbounded log tailing. Adding an operation is a contract amendment, never a flag.

## 4. Wire schema

- Versioned (`connector.v1`), strict (`extra="forbid"`), built from the existing pydantic
  models. UTC timestamps, deterministic ordering, explicit nulls.
- The connector never rewrites names, UIDs or evidence identity.
- **The in-process implementation must serialize through the wire schema** (encode, decode,
  compare). Otherwise "results unchanged" would hold in-process and break at the first network
  hop.

## 5. Semantics that must not depend on transport

1. **Failure is not emptiness.** A timeout, permission error, transport loss or backend error is
   a `ReadFailure` and maps to `BLOCKED_ACCESS` in the investigation; it is never `NO_DATA`, and
   never an empty list. A truncated or partial result says so (`truncated`, `partial_scopes`).
2. **Streams.** Each carries a monotonic cursor, at-least-once delivery, and a dedupe key (alert
   fingerprint plus `starts_at`; evidence id for objects). After a reconnect the Control Plane
   resumes from its last cursor.
3. **Continuity is declared, never assumed.** When the connector cannot guarantee it saw
   everything (restart, expired watch, dropped stream), it emits an explicit
   `GAP(from, to, reason)`. The Control Plane derives `alert_observation_start` (`W`) only from
   contiguous coverage between gaps.
4. **Audit.** Every request is logged at the Connector (operation, argument digest, bytes,
   outcome) and mirrored into the Control Plane's access ledger. The two are comparable.
5. **Redaction is a Connector policy** (allow-listed kinds and fields, log scrubbing hook). The
   Control Plane cannot request unredacted data.

## 6. Transport (deferred, not part of `connector.v1` semantics)

First an in-process Python protocol with the wire round trip of §4. Later gRPC over mTLS, the
Connector dialling out, static certificates first. Enrollment, rotation, Helm and the
Connect Cluster UI are separate work after this contract's gate.

## 7. Migration gate

The in-process path is accepted only when, for the 35 recorded scenarios (regression and replay),
it reproduces the **epistemic digests** of the current baseline (`.local/causal-closure/fx-full`),
including diagnosis status, claim level, state and timing status. Any difference is a defect;
there is no tolerance. Wire round-trip tests are part of the gate. The engine version does not
change. Existing `ReadTape` recordings must replay unchanged.

## 8. Owner decisions requested

1. Journal and object history stay Control Plane-owned (recommended, §3), rather than the
   Connector serving history.
2. `Secret` is excluded at the Connector; further kinds by policy, not by the Control Plane.
3. Streams carry explicit `GAP` records and `W` is derived from them (§5.3).
4. The polling loop of `ChangeWatcher` moves behind the Connector; the Control Plane only
   persists what arrives.
5. Live `query_traffic` and `query_traces` readers are out of scope for this contract's gate
   (they do not exist yet); they enter as amendments when built.
