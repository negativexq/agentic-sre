# Connector boundary contract (`connector.v1`)

Status: **APPROVED** by the owner (2026-09-30), including the five decisions of §8 as recommended.
Implementation proceeds in the order of §6 and §7. Amendments are made here first (same rule as
the M21 contracts).

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

## 8. Owner decisions (approved 2026-09-30, all as recommended)

1. Journal and object history stay Control Plane-owned (recommended, §3), rather than the
   Connector serving history.
2. `Secret` is excluded at the Connector; further kinds by policy, not by the Control Plane.
3. Streams carry explicit `GAP` records and `W` is derived from them (§5.3).
4. The polling loop of `ChangeWatcher` moves behind the Connector; the Control Plane only
   persists what arrives.
5. Live `query_traffic` and `query_traces` readers are out of scope for this contract's gate
   (they do not exist yet); they enter as amendments when built.

## 9. Implementation status (2026-09-30, steps 1-2)

Implemented in `packages/connector/` (`wire.py`, `service.py`, `client.py`): the eight request
operations (`capabilities`, `preflight`, `list_objects`, `list_events`, `query_logs`,
`query_resource_pressure`, `query_traffic`, `query_traces`), an in-process transport that passes
canonical JSON bytes, and reader adapters that implement the existing `ClusterReader`,
Prometheus, Loki and Tempo protocols. `service_from_environment` now builds the control plane's
readers from the connector client; the real readers exist only behind `connector_from_environment`.

Not yet through the connector (still direct, tracked for step 4): the Alertmanager webhook intake,
the Alertmanager coverage poll, and the `ChangeWatcher` polling loop; `list_alerts`, the `alerts`
stream and the `changes` stream are not implemented. `preflight` reports configured backends and
`reachable: null`; it does not probe. Every write path and query pass-through remains absent.

Migration gate (§7) result, 2026-09-30: every recorded connector-carried response of the 35 seen
scenarios (290,439 object bodies, 7 log responses, 14 trace responses) survives the wire equal;
the epistemic digest of six scenarios (18, 19, 22, 29, 83, 91) is identical whether the
observations pass through the wire codec or not. The tapes contain no resource-pressure, traffic
or Tempo-batch responses, so those types are covered by unit tests only, not by recorded data.

Findings recorded while implementing:

- **Lossy encoding is refused.** Pydantic's JSON mode turns `NaN` into `null`. The wire now
  dumps python-mode values through `json.dumps(allow_nan=False)` and each record set must decode
  back to an equal value, otherwise the read is an `EncodingError` failure (tested).
- **Failure type is now `ConnectorReadError`** on the control-plane side, carrying the backend's
  type in `remote_type`; `ProviderAdapter` therefore records that class name for provider
  failures instead of the backend's own exception class.
- **Pre-existing, not changed here:** `KubernetesClusterReader.list_events` skips a namespace
  whose listing fails and returns the rest, so a partial event listing is indistinguishable from
  a complete one. This violates §5.1 at the reader; fixing it changes read behaviour and needs
  its own amendment.

## 10. Streams as cursor-paged reads (amendment for roadmap step A4) — APPROVED

Approved by the owner on 2026-09-30 with the four decisions of §10.8 as recommended. It refines §3 and §5.2-5.3, which name two streams
(`alerts`, `changes`) without fixing their shape.

**10.1 Shape.** A stream is a pair of ordinary request operations, `read_alerts` and
`read_changes`, each taking `(cursor, limit)` and returning `(items, next_cursor, gaps)`. The
Control Plane asks; the Connector answers. This works unchanged over an outbound-only channel
(requests ride the connection the Connector opened), in-process, and over gRPC, and a resumed
read is the same call as a first read. There is no server push.

**10.2 Cursor and delivery.** A cursor is opaque to the Control Plane: `(epoch, sequence)`. The
Connector keeps a bounded buffer per stream and numbers items monotonically within an epoch; a
new epoch starts whenever the Connector restarts. The Control Plane persists a batch first and
commits its `next_cursor` afterwards, so delivery is at-least-once. Duplicates are removed by the
dedupe keys of §5.2 (alert fingerprint plus `starts_at` plus status; evidence id for objects).

**10.3 Gaps.** The Connector emits `Gap(reason, from, to)` and never guesses. Reasons:
`CONNECTOR_RESTART` (epoch changed), `BUFFER_EXPIRED` (the requested cursor fell out of the
buffer), `BACKEND_UNREACHABLE` (the Connector could not read the source). After a gap in
`changes` the Connector first sends a **snapshot** (a `begin`, the listed objects with per-scope
completeness as in `ObjectListing`, and an `end`), so the journal can tombstone exactly as
`ChangeWatcher` does today; a snapshot without an `end` proves nothing was deleted.

**10.4 Alert coverage stays a Control Plane computation.** The Connector polls Alertmanager
(`/api/v2/alerts`) at its own cadence and writes each poll into the alert stream as a
`Heartbeat(success|failure, at)` item next to the occurrences it saw. The Control Plane replays
those heartbeats through the existing segment rules (`alert_coverage.py`: a successful poll
extends the segment, a failed poll closes it, a gap breaks it), and a stream `Gap` breaks the
segment too. `W` is therefore derived from the same rules as today, from observations that
arrived in order.

**10.5 Short alerts.** A poll cannot see an alert that fires and resolves between two polls.
The Connector therefore also receives the Alertmanager webhook **inside the customer
environment** and feeds it into the same stream. The Control Plane no longer receives inbound
alert traffic from the customer's Alertmanager, and no Alertmanager credential leaves the
Connector.

**10.6 Bounds.** Per stream: a maximum buffer length, a maximum batch size, a maximum item
size. Exceeding the buffer is a `Gap`, not silent loss. Items are `WireModel`s and follow §4.

**10.7 Gate.** One transport-independent conformance suite (resume, duplicate delivery, restart,
expired cursor, snapshot without `end`, heartbeat-derived `W`) runs against the in-process
Connector now and against gRPC later; the two must not diverge. Existing coverage and alert
intake tests keep passing on the Connector path.

**10.8 Decisions (approved 2026-09-30, all as recommended).**
1. Streams are cursor-paged reads, not server push (§10.1).
2. The Connector receives the Alertmanager webhook locally and the Control Plane stops receiving
   it (§10.5); the legacy direct webhook stays available only in the non-connector mode.
3. Coverage heartbeats travel in the stream and `W` remains a Control Plane computation (§10.4).
4. Snapshots after a gap, with an explicit `end` (§10.3).

## 11. Implementation status of §10 (2026-09-30)

Implemented in-process: `read_alerts` and `read_changes` (`packages/connector/streams.py`, the
Connector's poll and webhook methods), the Control Plane's `StreamedClusterReader` mirror and
`AlertStreamConsumer` (`apps/control_plane/connector_intake.py`), with a 16-test conformance suite
(`tests/unit/test_connector_streams.py`): snapshot then deltas, deletions only for completely listed
scopes, backend gap blocks the listing, restart and expired-cursor gaps, snapshot without `end`,
idempotent replay, heartbeat-driven coverage that a replay cannot move backwards.

Stream mode is **opt-in** (`SRE_CONNECTOR_STREAMS=true`); the default keeps the direct webhook and
coverage poll until the transport step (roadmap A7), so the running deployment does not change
behaviour under this amendment. Not yet in place: the conformance suite against gRPC, the
Connector's standalone webhook listener (today the control plane's existing route hands the delivery
to the in-process Connector), and persistence of the consumer's cursor (a restart re-reads the
buffer and relies on the idempotent intake).

## 12. Transport: gRPC over mTLS, the Connector dials out (roadmap A7) — PROPOSED

Not approved; nothing below is implemented. It replaces the "deferred" transport of §6.

**12.1 Direction.** The Control Plane hosts a gRPC server. The Connector dials it and opens one
long-lived bidirectional stream, `connector.v1.Session/Open`. Requests travel Control Plane to
Connector over that stream and responses come back on it. The customer opens no inbound port and the
Control Plane never opens a connection into the customer environment.

**12.2 Framing.** Every stream message is `request_id` (8 bytes, big-endian) followed by the §4
canonical bytes, untouched: the transport neither parses nor re-encodes them. No `.proto` is
compiled; the service is a byte-stream contract served with generic handlers and identity
serializers (`protoc` is not part of this repository's toolchain). The message limit is
`MAX_RESPONSE_BYTES` plus 1 MiB.

**12.3 Identity.** mTLS is mandatory and the client certificate is required. A connector's identity
is a URI subject alternative name `connector:<connector_id>`. The Control Plane holds an allow-list
of connector ids; an unknown identity, an expired or untrusted certificate is refused. One live
session per id: a new session replaces the old one, which is closed. A reconnect is **not** a
restart: the Connector process keeps its epoch, so a stream cursor stays valid and no `Gap` is
declared.

**12.4 Static certificates (first version).** A CA, one server certificate for the Control Plane and
one leaf certificate per connector, generated by a helper (`packages/connector/pki.py`) with a 90-day
validity. No rotation and no revocation list; expiry is a hard failure shown in system status.
Private keys never leave the host that owns them. Enrollment and rotation remain roadmap A8.

**12.5 Request semantics.** A per-request timeout (30 s default) ends in `ConnectorUnavailable`; the
transport does not retry (reads are idempotent, but read identity and attempt budgets belong to the
caller). A request in flight when the session drops fails the same way, and a late response is
discarded.

**12.6 Reconnect and degraded start.** The Connector reconnects with exponential backoff from 1 s to
30 s with jitter; both sides send keepalive pings (20 s, 10 s timeout). The Control Plane **starts
without any Connector**: system status reports "connector not connected", no reader is attached,
and it never fails to start because a Connector is absent. On each connect it reads `capabilities`
and attaches (or replaces) its cluster and provider readers.

**12.7 Customer-side process.** `python -m packages.connector.agent`, configured by environment:
endpoint and certificate paths, watched namespaces, Alertmanager URL and token, the local webhook
listener address and its bearer token. It runs the poll loops of §10, the local webhook receiver
(§10.5, a stdlib HTTP server accepting authenticated POST only) and the mTLS client. It alone holds
backend credentials.

**12.8 Conformance.** The suites of §7 and §10 run parametrized by transport (in-process, and gRPC
on loopback with generated certificates); results must be identical. Transport-specific tests: an
unauthorized or expired certificate is refused, a second session replaces the first, a drop in the
middle of a request fails it, a reconnect resumes its cursor with no `Gap`, an oversize message is
refused.

**12.9 Not part of A7.** Flipping the default of `SRE_CONNECTOR_STREAMS` to on. It changes the
webhook response (incidents are created asynchronously, `incident_ids` is empty) that existing
tests pin, so it needs its own owner decision.

**12.10 Dependencies.** `grpcio` and `cryptography` become declared dependencies (both are already
installed as transitive ones). The lockfile refresh needs `uv`, which this environment does not
have; it is left to the owner.

**12.11 Decisions requested.**
1. The Connector dials out over one bidirectional stream; requests ride it back (§12.1).
2. Byte-stream framing with no compiled `.proto` (§12.2).
3. Identity by URI SAN, an allow-list of connector ids, one session per id, reconnect keeps the
   epoch (§12.3).
4. Static certificates from a helper, 90-day validity, no rotation in A7 (§12.4).
5. No transport retries; a timeout or a dropped session is `ConnectorUnavailable` (§12.5).
6. The Control Plane starts degraded without a Connector and attaches readers on connect (§12.6).
7. The stream-mode default flip stays out of A7 (§12.9).
