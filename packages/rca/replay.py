"""Offline RCA source rebuilt from one run's persisted evidence alone.

The run's ``EVIDENCE_GATHERED`` boundary names its window end, snapshot cycle
and provider capabilities, and its manifest names every row it may know.
Provider reads are served strictly from the run's tape: each request must be
the next recorded read, or replay stops with ``ReplayDivergence``. Nothing else
is read: no cluster, no clock, no provider.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.evidence_coverage import EvidenceCoverage
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.policy import (
    ReplayTrajectoryDivergence,
    ScriptedInvestigationPolicy,
)
from packages.rca.investigation.selection import DeterministicObservationPolicy
from packages.rca.investigation.tempo import (
    TempoSearchCompleteness,
    TempoSearchDiagnostics,
    TempoTraceBatch,
)
from packages.rca.live import LiveSource
from packages.rca.manifest import ManifestEntry, alert_from_payload, event_evidence_id
from packages.rca.model import (
    Alert,
    ClusterEvent,
    Diagnosis,
    EntityRef,
    InvestigationPolicyKind,
    InvestigationQuery,
    InvestigationResult,
    LogRecord,
    ObjectVersion,
    PodStatusObservation,
    ProviderReadFailure,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)
from packages.rca.provider_adapter import (
    ProviderCallerClass,
    ProviderIntegrityError,
    loki_descriptor,
    provider_query_key,
    resource_pressure_descriptor,
    tempo_descriptor,
    traffic_descriptor,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from packages.rca.investigation.environment import InvestigationBackend


_T = TypeVar("_T")


class ReplayDivergence(ProviderIntegrityError):
    """A replayed provider request is not the next read the run recorded.

    It is never missing data: the replayed execution left the recorded one.
    """

    def __init__(
        self,
        run_id: str,
        reason: str,
        *,
        requested: tuple[str, str, str],
        recorded: tuple[int, str, str, str] | None,
    ) -> None:
        self.run_id = run_id
        self.reason = reason
        # (caller_class, capability, query_key)
        self.requested = requested
        # (sequence, caller_class, capability, query_key) of the next tape row
        self.recorded = recorded
        next_row = (
            f"next recorded seq {recorded[0]} {recorded[1]}/{recorded[2]} key={recorded[3]}"
            if recorded is not None
            else "tape exhausted"
        )
        super().__init__(
            f"run {run_id}: {reason}; requested {requested[0]}/{requested[1]} "
            f"key={requested[2]}; {next_row}"
        )


@dataclass(frozen=True)
class _TapeRow:
    sequence: int
    read_id: int
    caller_class: str
    capability: str
    query_key: str
    descriptor: Mapping[str, Any]
    status: str
    observation: Any
    error_type: str | None
    error_message: str | None


@dataclass
class _Cursor:
    position: int


def _load_tape(session_factory: Callable[[], Session], run_id: str) -> tuple[_TapeRow, ...]:
    """The run's tape in sequence order, checked for replayable shape.

    Sequences must be exactly 1..n, and CAPTURE reads may only form the
    leading prefix (capture -> manifest -> RCA).
    """
    from packages.storage.manifest import ReplayDataError
    from packages.storage.repositories import InvestigationReadRepository

    with session_factory() as session:
        rows = tuple(
            _TapeRow(
                sequence=row.sequence,
                read_id=row.read_id,
                caller_class=row.caller_class,
                capability=row.capability,
                query_key=row.query_key,
                descriptor=row.query_descriptor,
                status=row.status,
                observation=row.observation,
                error_type=row.error_type,
                error_message=row.error_message,
            )
            for row in InvestigationReadRepository(session).list_for_run(run_id)
        )
    if [row.sequence for row in rows] != list(range(1, len(rows) + 1)):
        raise ReplayDataError(f"run {run_id} tape sequences are not contiguous from 1")
    prefix = _capture_prefix(rows)
    if any(row.caller_class == "CAPTURE" for row in rows[prefix:]):
        raise ReplayDataError(f"run {run_id} tape has a CAPTURE read after RCA reads began")
    return rows


def _capture_prefix(rows: Sequence[_TapeRow]) -> int:
    prefix = 0
    while prefix < len(rows) and rows[prefix].caller_class == "CAPTURE":
        prefix += 1
    return prefix


@dataclass(frozen=True)
class ReplayProviderAdapter:
    """``ProviderAdapter``'s interface served from one run's recorded tape.

    The leading CAPTURE reads were consumed by the capture stage (their results
    are manifest evidence), so the cursor starts after them. Every request must
    match the next row exactly (caller class, capability, query key, canonical
    descriptor); otherwise ``ReplayDivergence`` and the cursor does not move.
    Nothing is written and no provider is called.
    """

    run_id: str
    caller_class: ProviderCallerClass
    provider_capabilities: tuple[str, ...]
    _rows: tuple[_TapeRow, ...] = field(repr=False)
    _cursor: _Cursor = field(repr=False)

    @classmethod
    def from_run(
        cls,
        run_id: str,
        *,
        session_factory: Callable[[], Session],
        caller_class: ProviderCallerClass,
    ) -> ReplayProviderAdapter:
        """Load the run's frozen capabilities and tape; start after the CAPTURE prefix."""
        from packages.storage.manifest import load_run_boundary

        with session_factory() as session:
            capabilities = load_run_boundary(session, run_id).provider_capabilities
        rows = _load_tape(session_factory, run_id)
        return cls(run_id, caller_class, capabilities, rows, _Cursor(_capture_prefix(rows)))

    def for_caller(self, caller_class: ProviderCallerClass) -> ReplayProviderAdapter:
        """Another caller class on the same tape and cursor."""
        return ReplayProviderAdapter(
            self.run_id, caller_class, self.provider_capabilities, self._rows, self._cursor
        )

    @property
    def next_sequence(self) -> int | None:
        """Sequence of the next row replay expects, ``None`` once the tape is consumed."""
        position = self._cursor.position
        return self._rows[position].sequence if position < len(self._rows) else None

    @property
    def recorded_query_keys(self) -> tuple[str, ...]:
        """Query keys of the recorded RCA-stage reads (after the CAPTURE prefix), in order."""
        return tuple(row.query_key for row in self._rows[_capture_prefix(self._rows) :])

    @property
    def consumed_query_keys(self) -> tuple[str, ...]:
        """Query keys replay has served so far, in order."""
        start = _capture_prefix(self._rows)
        return tuple(row.query_key for row in self._rows[start : self._cursor.position])

    def supports(self, capability: str) -> bool:
        return capability in self.provider_capabilities

    def capabilities(self) -> tuple[str, ...]:
        return self.provider_capabilities

    def query_resource_pressure(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        *,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[ResourcePressure, ...] | ProviderReadFailure:
        return self._replay(
            "resource_pressure",
            "resource_pressure",
            resource_pressure_descriptor(target, query),
            descriptor_id,
            observation_identity,
            lambda value: tuple(ResourcePressure.model_validate(item) for item in value),
        )

    def query_traffic(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        *,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[TrafficObservation, ...] | ProviderReadFailure:
        return self._replay(
            "traffic",
            "traffic",
            traffic_descriptor(target, query),
            descriptor_id,
            observation_identity,
            lambda value: tuple(TrafficObservation.model_validate(item) for item in value),
        )

    def query_tempo(
        self,
        target: EntityRef,
        query: InvestigationQuery,
        *,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[TraceSpanObservation, ...] | TempoTraceBatch | ProviderReadFailure:
        return self._replay(
            "runtime_traces",
            "tempo_traces",
            tempo_descriptor(target, query),
            descriptor_id,
            observation_identity,
            _decode_tempo,
        )

    def query_loki(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> list[LogRecord] | ProviderReadFailure:
        result = self.query_loki_with_read_id(
            services,
            starts_at,
            ends_at,
            limit=limit,
            descriptor_id=descriptor_id,
            observation_identity=observation_identity,
        )
        return result if isinstance(result, ProviderReadFailure) else result[0]

    def query_loki_with_read_id(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
        descriptor_id: str | None = None,
        observation_identity: str | None = None,
    ) -> tuple[list[LogRecord], int] | ProviderReadFailure:
        read_id: list[int] = []

        def decode(value: Any) -> list[LogRecord]:
            return [LogRecord.model_validate(item) for item in value]

        records = self._replay(
            "logs",
            "loki_logs",
            loki_descriptor(services, starts_at, ends_at, limit),
            descriptor_id,
            observation_identity,
            decode,
            on_match=read_id.append,
        )
        return records if isinstance(records, ProviderReadFailure) else (records, read_id[0])

    def _replay(
        self,
        supports_name: str,
        capability: str,
        descriptor: dict[str, Any],
        descriptor_id: str | None,
        observation_identity: str | None,
        decode: Callable[[Any], _T],
        *,
        on_match: Callable[[int], None] | None = None,
    ) -> _T | ProviderReadFailure:
        requested = (
            self.caller_class,
            capability,
            provider_query_key(
                descriptor, descriptor_id=descriptor_id, observation_identity=observation_identity
            ),
        )
        position = self._cursor.position
        row = self._rows[position] if position < len(self._rows) else None
        recorded = (
            (row.sequence, row.caller_class, row.capability, row.query_key)
            if row is not None
            else None
        )
        if not self.supports(supports_name):
            raise ReplayDivergence(
                self.run_id,
                f"{supports_name} was not a provider capability of the recorded run",
                requested=requested,
                recorded=recorded,
            )
        if row is None:
            raise ReplayDivergence(
                self.run_id, "no recorded read remains", requested=requested, recorded=None
            )
        if (row.caller_class, row.capability, row.query_key) != requested:
            raise ReplayDivergence(
                self.run_id,
                "request is not the next recorded read",
                requested=requested,
                recorded=recorded,
            )
        if dict(row.descriptor) != descriptor:
            raise ReplayDivergence(
                self.run_id,
                "request descriptor differs from the recorded read",
                requested=requested,
                recorded=recorded,
            )
        if row.status == "ERROR":
            result: _T | ProviderReadFailure = ProviderReadFailure(
                capability=row.capability,
                error_type=row.error_type or "",
                error_message=row.error_message or "",
            )
        else:
            result = _decoded(self.run_id, row, decode)
        self._cursor.position = position + 1
        if on_match is not None:
            on_match(row.read_id)
        return result


def _decoded[T](run_id: str, row: _TapeRow, decode: Callable[[Any], T]) -> T:
    from packages.storage.manifest import ReplayTapeCorrupt

    try:
        return decode(row.observation)
    except (TypeError, ValueError, KeyError) as error:
        raise ReplayTapeCorrupt(
            f"run {run_id} tape seq {row.sequence} observation cannot be decoded: "
            f"{type(error).__name__}"
        ) from error


def _decode_tempo(value: Any) -> tuple[TraceSpanObservation, ...] | TempoTraceBatch:
    """Tempo results are persisted either as a span list or as a batch mapping."""
    if isinstance(value, list):
        return tuple(TraceSpanObservation.model_validate(item) for item in value)
    diagnostics = value["diagnostics"]
    return TempoTraceBatch(
        spans=tuple(TraceSpanObservation.model_validate(item) for item in value["spans"]),
        diagnostics=TempoSearchDiagnostics(
            completeness=TempoSearchCompleteness(diagnostics["completeness"]),
            candidate_trace_ids=tuple(diagnostics["candidate_trace_ids"]),
            search_limit_reached=diagnostics["search_limit_reached"],
            inspected_traces=diagnostics["inspected_traces"],
            inspected_bytes=diagnostics["inspected_bytes"],
            completed_jobs=diagnostics["completed_jobs"],
            total_jobs=diagnostics["total_jobs"],
            fetched_trace_ids=tuple(diagnostics["fetched_trace_ids"]),
            missing_trace_ids=tuple(diagnostics["missing_trace_ids"]),
        ),
    )


@dataclass(frozen=True)
class ReplaySource:
    """One run's frozen base evidence: manifest members, snapshot cycle, boundary."""

    run_id: str
    window_end: datetime
    snapshot_cycle_id: int | None
    manifest: tuple[ManifestEntry, ...]
    snapshot_objects: tuple[dict[str, Any], ...]
    # The live run's configured provider capabilities, as its boundary froze them.
    provider_capabilities: tuple[str, ...]
    provider_adapter: ReplayProviderAdapter
    _base: LiveSource

    @classmethod
    def from_run(cls, run_id: str, *, session_factory: sessionmaker[Session]) -> ReplaySource:
        """Load the run's boundary, manifest members and exact snapshot cycle."""
        from packages.storage.manifest import load_manifest, load_replay_run

        with session_factory() as session:
            boundary, members = load_replay_run(session, run_id)
            manifest = load_manifest(session, run_id)
        snapshot = members.snapshot
        # The same caller the live run's engine used; investigation derives its own.
        adapter = ReplayProviderAdapter.from_run(
            run_id, session_factory=session_factory, caller_class="ENGINE"
        )
        base = LiveSource(
            incident=str(boundary.incident_id),
            alert_items=[alert_from_payload(item) for item in members.alerts],
            journal=list(members.journal),
            current_objects=list(snapshot.objects) if snapshot else [],
            event_bodies=[body for _, body in members.events],
            event_evidence_ids=[event_evidence_id(version_id) for version_id, _ in members.events],
            error_items=list(members.logs),
            trace_items=list(members.traces),
            observed_at=boundary.window_end,
            # The live run used its snapshot exactly when it captured one.
            current_is_live=snapshot is not None,
            provider_adapter=adapter,
            lifecycle_records=members.lifecycle,
            snapshot_cycle_id=snapshot.cycle_id if snapshot else None,
            snapshot_observed_at=snapshot.observed_at if snapshot else None,
            # Exactly the coverage the live run froze; never recomputed from segments.
            alert_coverage=boundary.alert_coverage,
            evidence_coverage=boundary.evidence_coverage,
        )
        return cls(
            run_id=run_id,
            window_end=boundary.window_end,
            snapshot_cycle_id=boundary.snapshot_cycle_id,
            manifest=manifest,
            snapshot_objects=snapshot.objects if snapshot else (),
            provider_capabilities=boundary.provider_capabilities,
            provider_adapter=adapter,
            _base=base,
        )

    def incident_id(self) -> str:
        return self._base.incident_id()

    def observation_cutoff(self) -> datetime | None:
        return self.window_end

    def alerts(self) -> Sequence[Alert]:
        return self._base.alerts()

    def alert_observation_start(self) -> datetime | None:
        return self._base.alert_observation_start()

    def evidence_coverage_record(self) -> EvidenceCoverage | None:
        return self._base.evidence_coverage_record()

    def object_history(self) -> Mapping[EntityRef, Sequence[ObjectVersion]]:
        return self._base.object_history()

    def events(self) -> Sequence[ClusterEvent]:
        return self._base.events()

    def logs(self, service: str, *, limit: int = 20) -> Sequence[dict[str, Any]]:
        return self._base.logs(service, limit=limit)

    def error_logs(self) -> Sequence[LogRecord]:
        return self._base.error_logs()

    def pod_status_observations(self) -> Sequence[PodStatusObservation]:
        return self._base.pod_status_observations()

    def traffic_observations(self) -> Sequence[TrafficObservation]:
        # The live source answers this without any provider; replay matches it.
        return self._base.traffic_observations()

    def trace_observations(self) -> Sequence[TraceSpanObservation]:
        # The live source answers this without any provider; replay matches it.
        return self._base.trace_observations()

    # The provider-backed methods run ``LiveSource``'s own code over the tape
    # adapter, so support checks and backend construction match the live run.
    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> Sequence[ResourcePressure] | ProviderReadFailure:
        return self._base.resource_pressure(pods, since)

    def supports(self, capability: str) -> bool:
        return self._base.supports(capability)

    def supports_typed_runtime(self, capability: str) -> bool:
        return self._base.supports_typed_runtime(capability)

    def investigation_backend(self) -> InvestigationBackend:
        return self._base.investigation_backend()


class ReplayModeUnsupported(ValueError):
    """The requested replay mode is not implemented."""


class ReplayEngineIncompatible(ReplayModeUnsupported):
    """The run was diagnosed by other RCA engine semantics than this replay runs."""


def _require_replayable_engine(session: Session, run_id: str) -> None:
    """Every replay re-runs the RCA engine, so it must be the engine the run recorded."""
    from sqlalchemy import select

    from packages.rca.engine import RCA_ENGINE_VERSION
    from packages.storage.manifest import ReplayDataError
    from packages.storage.models import DiagnosisRow

    row = session.scalars(select(DiagnosisRow).where(DiagnosisRow.run_id == run_id)).first()
    if row is None:
        raise ReplayDataError(f"run {run_id} has no persisted diagnosis")
    if row.engine_version is None:
        raise ReplayEngineIncompatible(f"run {run_id} recorded no RCA engine version")
    if row.engine_version != RCA_ENGINE_VERSION:
        raise ReplayEngineIncompatible(
            f"run {run_id} was diagnosed by RCA engine {row.engine_version}; "
            f"this replay runs RCA engine {RCA_ENGINE_VERSION}"
        )


@dataclass(frozen=True)
class TrajectoryReplay:
    """One completed trajectory replay: its digest and the state that proves it."""

    digest: str
    result: InvestigationResult
    source: ReplaySource
    policy: ScriptedInvestigationPolicy


def replay_trajectory(run_id: str, *, session_factory: sessionmaker[Session]) -> TrajectoryReplay:
    """Replay the run's recorded trajectory over its frozen evidence and tape.

    One ``ReplaySource`` (and so one provider cursor) serves the whole run. The
    recorded audits drive every turn; the replay succeeds only if all of them
    and every recorded non-CAPTURE read were consumed and the recorded terminal
    was reached. Nothing is written.
    """
    from packages.storage.trajectory import load_trajectory

    with session_factory() as session:
        recorded = load_trajectory(session, run_id)
        _require_replayable_engine(session, run_id)
    source = ReplaySource.from_run(run_id, session_factory=session_factory)
    contract = recorded.contract
    policy = ScriptedInvestigationPolicy.from_trajectory(
        recorded.result.action_audits,
        semantic_kind=contract.policy_kind,
        counts_as_model=contract.counts_as_model,
        terminal=contract.terminal,
    )
    result = investigate_diagnosis(
        source, policy=policy, config=recorded.config, started_at=source.window_end
    )
    if not policy.exhausted:
        raise ReplayTrajectoryDivergence(
            f"run {run_id}: {len(policy.recorded_audits) - policy.index} recorded action(s) "
            "were never replayed"
        )
    if (result.stop_reason, result.turns) != (
        contract.terminal.stop_reason,
        contract.terminal.turns,
    ):
        raise ReplayTrajectoryDivergence(
            f"run {run_id}: replay ended {result.stop_reason.value} after {result.turns} turn(s); "
            f"recorded {contract.terminal.stop_reason.value} after {contract.terminal.turns}"
        )
    remaining = source.provider_adapter.next_sequence
    if remaining is not None:
        raise ReplayTrajectoryDivergence(
            f"run {run_id}: recorded provider reads from sequence {remaining} were never replayed"
        )
    return TrajectoryReplay(diagnosis_epistemic_digest(result.diagnosis), result, source, policy)


_SELECTORS: dict[InvestigationPolicyKind, Callable[[], Any]] = {
    InvestigationPolicyKind.OBSERVATION_SELECTOR: DeterministicObservationPolicy,
    InvestigationPolicyKind.INTENT_SELECTOR: DeterministicIntentPolicy,
}


@dataclass(frozen=True)
class SelectorReplay:
    """One completed selector replay: its digest and the sequences it matched."""

    digest: str
    result: InvestigationResult
    source: ReplaySource
    policy: Any
    recorded_query_keys: tuple[str, ...]
    replayed_query_keys: tuple[str, ...]


def replay_selector(run_id: str, *, session_factory: sessionmaker[Session]) -> SelectorReplay:
    """Re-run the run's deterministic selector over its frozen evidence and tape.

    Nothing recorded chooses an action: the recorded selector family runs again
    with the recorded config. It must select the recorded actions, read exactly
    the recorded query-key sequence and end at the recorded terminal.
    """
    from packages.storage.trajectory import load_trajectory

    with session_factory() as session:
        recorded = load_trajectory(session, run_id)
        _require_replayable_engine(session, run_id)
    contract = recorded.contract
    selector = _SELECTORS.get(contract.policy_kind)
    if selector is None:
        raise ReplayModeUnsupported(
            f"run {run_id} was recorded by a {contract.policy_kind.value} policy; "
            "selector replay needs a deterministic selector trajectory"
        )
    source = ReplaySource.from_run(run_id, session_factory=session_factory)
    policy = selector()
    result = investigate_diagnosis(
        source,
        policy=policy,
        config=recorded.config,
        started_at=source.window_end,
        recorded_terminal=contract.terminal,
    )
    recorded_actions = [audit.action for audit in recorded.result.action_audits]
    replayed_actions = [audit.action for audit in result.action_audits]
    if replayed_actions != recorded_actions:
        raise ReplayTrajectoryDivergence(
            f"run {run_id}: the selector chose {len(replayed_actions)} action(s) that differ "
            f"from the {len(recorded_actions)} recorded"
        )
    if (result.stop_reason, result.turns) != (
        contract.terminal.stop_reason,
        contract.terminal.turns,
    ):
        raise ReplayTrajectoryDivergence(
            f"run {run_id}: selector replay ended {result.stop_reason.value} after "
            f"{result.turns} turn(s); recorded {contract.terminal.stop_reason.value} "
            f"after {contract.terminal.turns}"
        )
    adapter = source.provider_adapter
    if adapter.consumed_query_keys != adapter.recorded_query_keys:
        raise ReplayTrajectoryDivergence(
            f"run {run_id}: recorded provider reads from sequence {adapter.next_sequence} "
            "were never selected"
        )
    return SelectorReplay(
        diagnosis_epistemic_digest(result.diagnosis),
        result,
        source,
        policy,
        adapter.recorded_query_keys,
        adapter.consumed_query_keys,
    )


class ReplayUnconsumedReads(RuntimeError):
    """A base replay finished without reading every recorded ENGINE read."""


@dataclass(frozen=True)
class BaseReplay:
    """A replayed normal (non-investigation) run and its epistemic digest."""

    digest: str
    diagnosis: Diagnosis
    source: ReplaySource


def replay_base(run_id: str, *, session_factory: sessionmaker[Session]) -> BaseReplay:
    """Replay a run that diagnosed without investigation (M20.1b).

    The live service diagnosed such a run with ``diagnose(source, config=EngineConfig())``
    and no investigator; replay runs exactly that over the run's frozen evidence
    and tape. It refuses a run with an investigation trajectory (replay it with
    ``trajectory``), a non-deterministic or model-assisted diagnosis, or another
    engine config, and it requires every recorded ENGINE read to be consumed.
    Nothing is written; no cluster, clock or provider is read.
    """
    from sqlalchemy import select

    from packages.rca.engine import EngineConfig, diagnose
    from packages.rca.investigation.state import rca_config_digest
    from packages.storage.manifest import ReplayDataError
    from packages.storage.models import DiagnosisRow
    from packages.storage.trajectory import ReplayTrajectoryMissing, load_trajectory

    config = EngineConfig()
    with session_factory() as session:
        try:
            load_trajectory(session, run_id)
        except ReplayTrajectoryMissing:
            pass
        except ReplayDataError as error:
            raise ReplayModeUnsupported(
                f"run {run_id} has an investigation artifact; base replay does not apply"
            ) from error
        else:
            raise ReplayModeUnsupported(
                f"run {run_id} has an investigation trajectory; replay it with 'trajectory'"
            )
        row = session.scalars(select(DiagnosisRow).where(DiagnosisRow.run_id == run_id)).first()
        if row is None:
            raise ReplayDataError(f"run {run_id} has no persisted diagnosis")
        _require_replayable_engine(session, run_id)
        mode, calls, recorded_config = (
            row.document.get("mode"),
            row.document.get("model_calls"),
            row.config_digest,
        )
    if mode != "deterministic" or calls:
        raise ReplayModeUnsupported(
            f"run {run_id} was diagnosed in mode {mode!r} with {calls} model call(s); "
            "base replay reproduces only deterministic runs"
        )
    if recorded_config is not None and recorded_config != rca_config_digest(config, None):
        raise ReplayModeUnsupported(
            f"run {run_id} used engine config {recorded_config}, not this replay's config"
        )
    source = ReplaySource.from_run(run_id, session_factory=session_factory)
    diagnosis = diagnose(source, config=config)
    remaining = source.provider_adapter.next_sequence
    if remaining is not None:
        raise ReplayUnconsumedReads(
            f"run {run_id}: recorded provider reads from sequence {remaining} were never replayed"
        )
    return BaseReplay(diagnosis_epistemic_digest(diagnosis), diagnosis, source)


def replay_run(
    run_id: str, mode: str = "trajectory", *, session_factory: sessionmaker[Session]
) -> str:
    """Replay one recorded run offline and return its epistemic digest.

    ``trajectory`` plays the recorded actions; ``selector`` re-runs the recorded
    deterministic selector and checks it chooses the same reads; ``base``
    re-runs a normal (non-investigation) deterministic diagnosis.
    """
    if mode == "trajectory":
        return replay_trajectory(run_id, session_factory=session_factory).digest
    if mode == "selector":
        return replay_selector(run_id, session_factory=session_factory).digest
    if mode == "base":
        return replay_base(run_id, session_factory=session_factory).digest
    raise ReplayModeUnsupported(f"replay mode {mode!r} is not supported")


__all__ = [
    "BaseReplay",
    "ReplayDivergence",
    "ReplayEngineIncompatible",
    "ReplayModeUnsupported",
    "ReplayProviderAdapter",
    "ReplaySource",
    "ReplayTrajectoryDivergence",
    "ReplayUnconsumedReads",
    "SelectorReplay",
    "TrajectoryReplay",
    "replay_base",
    "replay_run",
    "replay_selector",
    "replay_trajectory",
]
