"""M19-3.15: provider reads are served strictly from the run's tape."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker
from test_evidence_manifest import POD as _POD
from test_evidence_manifest import Seen
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture

import packages.rca.live as live_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.investigation.environment import (
    LokiInvestigationBackend,
    PrometheusInvestigationBackend,
    TempoInvestigationBackend,
)
from packages.rca.investigation.tempo import (
    TempoSearchCompleteness,
    TempoSearchDiagnostics,
    TempoTraceBatch,
)
from packages.rca.live import LiveSource
from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    LogRecord,
    ProviderReadFailure,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)
from packages.rca.provider_adapter import (
    PROVIDER_CAPABILITIES,
    ProviderAdapter,
    ProviderCallerClass,
    ProviderReaders,
)
from packages.rca.replay import ReplayDivergence, ReplayProviderAdapter, ReplaySource
from packages.storage.manifest import ManifestRequest, ReplayDataError, build_manifest
from packages.storage.models import InvestigationReadRow
from packages.storage.repositories import DiagnosisRepository, IncidentRepository

POD: dict[str, Any] = _POD
TARGET = EntityRef(kind="Pod", name=POD["metadata"]["name"], namespace="sre-demo")
WORKLOAD = EntityRef(kind="Deployment", name="payment-service", namespace="sre-demo")
WINDOW_END = T0 + timedelta(minutes=30)


def q(minute: int) -> InvestigationQuery:
    return InvestigationQuery(
        start=T0 + timedelta(minutes=minute),
        end=T0 + timedelta(minutes=minute + 5),
        limit=16,
    )


class Offline:
    """Fake readers that count calls and can be switched off (network = 0)."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.offline = False

    def _hit(self) -> int:
        if self.offline:
            raise AssertionError("replay must not call a provider reader")
        self.calls += 1
        if self.fail:
            raise ConnectionError("provider unavailable at 10.0.0.1:9090")
        return self.calls


class FakePrometheus(Offline):
    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]:
        n = self._hit()
        return (
            ResourcePressure(
                pod=target,
                container="app",
                resource="memory",
                baseline=0.25,
                peak=0.5 + n / 100,
                at=query.end,
                evidence_id=f"prom:rp:{target.name}:{n}",
                sample_count=3,
                sample_start=query.start,
                sample_end=query.end,
            ),
        )

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]:
        n = self._hit()
        assert query.end is not None
        return (
            TrafficObservation(
                entity=target,
                metric="http_requests_per_second",
                at=query.end,
                value=float(n),
                evidence_id=f"prom:traffic:{target.name}:{n}",
            ),
        )


class FakeTempo(Offline):
    batch = False

    def query(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TraceSpanObservation, ...] | TempoTraceBatch:
        n = self._hit()
        assert query.start is not None
        spans = (
            TraceSpanObservation(
                trace_id=f"trace-{n}",
                span_id=f"span-{n}",
                service=target.name,
                start_at=query.start + timedelta(minutes=1),
                end_at=query.start + timedelta(minutes=2),
                semantic_attributes={
                    "k8s.namespace.name": target.namespace,
                    f"k8s.{target.kind.lower()}.name": target.name,
                },
                evidence_id=f"tempo:trace-{n}:span-{n}",
            ),
        )
        if not self.batch:
            return spans
        return TempoTraceBatch(
            spans=spans,
            diagnostics=TempoSearchDiagnostics(
                completeness=TempoSearchCompleteness.TRUNCATED,
                candidate_trace_ids=(f"trace-{n}",),
                search_limit_reached=True,
                inspected_traces=4,
                inspected_bytes=None,
                completed_jobs=2,
                total_jobs=3,
                fetched_trace_ids=(f"trace-{n}",),
                missing_trace_ids=(),
            ),
        )


class FakeLoki(Offline):
    def error_logs(
        self,
        services: Any,
        starts_at: Any,
        ends_at: Any,
        *,
        limit: int | None = None,
    ) -> list[LogRecord]:
        n = self._hit()
        return [
            LogRecord(
                service="payment-service",
                at=starts_at + timedelta(minutes=1),
                severity="error",
                message=f"timeout calling bank #{n}",
                evidence_id=f"loki:payment-service:{n}:0",
            )
        ]


class Readers:
    def __init__(self) -> None:
        self.prometheus = FakePrometheus()
        self.tempo = FakeTempo()
        self.loki = FakeLoki()

    def configured(self, capabilities: tuple[str, ...] = PROVIDER_CAPABILITIES) -> ProviderReaders:
        return ProviderReaders(
            prometheus=self.prometheus if "traffic" in capabilities else None,
            tempo=self.tempo if "runtime_traces" in capabilities else None,
            loki=self.loki if "logs" in capabilities else None,
        )

    def go_offline(self) -> None:
        for reader in (self.prometheus, self.tempo, self.loki):
            reader.offline = True


class Tape:
    """A persisted run boundary plus live-recorded tape rows for that run."""

    def __init__(self, factory: sessionmaker[Session], incident_id: Any, readers: Readers) -> None:
        self.factory = factory
        self.incident_id = incident_id
        self.readers = readers

    def boundary(self, run_id: str, capabilities: tuple[str, ...] = PROVIDER_CAPABILITIES) -> str:
        with self.factory() as session:
            incident = IncidentRepository(session).get(self.incident_id)
            assert incident is not None
            correlation_id = incident.correlation_id
        build_manifest(
            self.factory,
            ManifestRequest(
                run_id=run_id,
                incident_id=self.incident_id,
                correlation_id=correlation_id,
                starts_at=T0,
                ends_at=WINDOW_END,
                window_end=WINDOW_END,
                namespaces=frozenset({"sre-demo"}),
                journal_namespaces=frozenset({"sre-demo"}),
                snapshot_cycle_id=None,
                listed_objects=0,
                provider_capabilities=capabilities,
            ),
            timestamp=WINDOW_END,
        )
        return run_id

    def live(
        self,
        run_id: str,
        caller: ProviderCallerClass,
        capabilities: tuple[str, ...] = PROVIDER_CAPABILITIES,
    ) -> ProviderAdapter:
        return ProviderAdapter(run_id, caller, self.factory, self.readers.configured(capabilities))

    def replay(self, run_id: str, caller: ProviderCallerClass) -> ReplayProviderAdapter:
        return ReplayProviderAdapter.from_run(
            run_id, session_factory=self.factory, caller_class=caller
        )

    def rows(self, run_id: str) -> list[tuple[Any, ...]]:
        with self.factory() as session:
            return [
                (
                    row.read_id,
                    row.sequence,
                    row.caller_class,
                    row.capability,
                    row.query_key,
                    row.query_descriptor,
                    row.started_at,
                    row.finished_at,
                    row.committed_at,
                    row.status,
                    row.observation,
                    row.evidence_ids,
                    row.error_type,
                    row.error_message,
                )
                for row in session.scalars(
                    select(InvestigationReadRow)
                    .where(InvestigationReadRow.run_id == run_id)
                    .order_by(InvestigationReadRow.sequence)
                )
            ]


@pytest.fixture
def tape(setup: Any) -> Tape:  # noqa: F811
    factory, _, _, incident_id = setup
    return Tape(factory, incident_id, Readers())


def test_exact_next_read_replays_the_recorded_success(tape: Tape) -> None:
    run = tape.boundary("run-a")
    recorded = tape.live(run, "INVESTIGATION").query_traffic(TARGET, q(10))
    replay = tape.replay(run, "INVESTIGATION")
    assert replay.next_sequence == 1
    assert replay.query_traffic(TARGET, q(10)) == recorded
    assert isinstance(recorded, tuple) and recorded
    assert replay.next_sequence is None


def test_mismatched_request_diverges_and_leaves_the_cursor(tape: Tape) -> None:
    run = tape.boundary("run-a")
    recorded = tape.live(run, "INVESTIGATION").query_traffic(TARGET, q(10))
    replay = tape.replay(run, "INVESTIGATION")
    with pytest.raises(ReplayDivergence) as raised:
        replay.query_traffic(TARGET, q(11))
    assert raised.value.run_id == run
    assert raised.value.recorded is not None and raised.value.recorded[0] == 1
    assert raised.value.requested[2] != raised.value.recorded[3]
    assert replay.next_sequence == 1
    assert replay.query_traffic(TARGET, q(10)) == recorded


def test_out_of_order_request_never_scans_ahead(tape: Tape) -> None:
    run = tape.boundary("run-a")
    live = tape.live(run, "INVESTIGATION")
    first = live.query_traffic(TARGET, q(10))
    second = live.query_traffic(TARGET, q(11))
    replay = tape.replay(run, "INVESTIGATION")
    with pytest.raises(ReplayDivergence):
        replay.query_traffic(TARGET, q(11))  # recorded, but only at sequence 2
    assert replay.next_sequence == 1
    assert replay.query_traffic(TARGET, q(10)) == first
    assert replay.query_traffic(TARGET, q(11)) == second


def test_unknown_and_exhausted_requests_diverge(tape: Tape) -> None:
    run = tape.boundary("run-a")
    recorded = tape.live(run, "ENGINE").query_resource_pressure(TARGET, q(10))
    replay = tape.replay(run, "ENGINE")
    with pytest.raises(ReplayDivergence, match="not the next recorded read"):
        replay.query_resource_pressure(TARGET, q(10), descriptor_id="never-recorded")
    assert replay.query_resource_pressure(TARGET, q(10)) == recorded
    with pytest.raises(ReplayDivergence, match="no recorded read remains") as raised:
        replay.query_resource_pressure(TARGET, q(10))
    assert raised.value.recorded is None


def test_recorded_error_replays_as_the_same_typed_failure(tape: Tape) -> None:
    run = tape.boundary("run-a")
    tape.readers.prometheus.fail = True
    recorded = tape.live(run, "ENGINE").query_resource_pressure(TARGET, q(10))
    assert isinstance(recorded, ProviderReadFailure)
    replayed = tape.replay(run, "ENGINE").query_resource_pressure(TARGET, q(10))
    assert isinstance(replayed, ProviderReadFailure)
    assert replayed == recorded
    assert replayed.error_type == "ConnectionError"
    assert replayed.capability == "resource_pressure"
    assert replayed.error_message == recorded.error_message and replayed.error_message


def test_repeated_identical_queries_are_distinct_reads(tape: Tape) -> None:
    run = tape.boundary("run-a")
    live = tape.live(run, "INVESTIGATION")
    first = live.query_traffic(TARGET, q(10))
    second = live.query_traffic(TARGET, q(10))
    assert first != second
    keys = {row[4] for row in tape.rows(run)}
    assert len(keys) == 1
    replay = tape.replay(run, "INVESTIGATION")
    assert replay.query_traffic(TARGET, q(10)) == first
    assert replay.query_traffic(TARGET, q(10)) == second


def test_another_operation_with_the_same_key_diverges(tape: Tape) -> None:
    run = tape.boundary("run-a")
    recorded = tape.live(run, "INVESTIGATION").query_traffic(
        TARGET, q(10), descriptor_id="shared-key"
    )
    replay = tape.replay(run, "INVESTIGATION")
    for other in (replay.query_resource_pressure, replay.query_tempo):
        with pytest.raises(ReplayDivergence):
            other(TARGET, q(10), descriptor_id="shared-key")
        assert replay.next_sequence == 1
    assert replay.query_traffic(TARGET, q(10), descriptor_id="shared-key") == recorded


def test_another_caller_class_diverges(tape: Tape) -> None:
    run = tape.boundary("run-a")
    recorded = tape.live(run, "INVESTIGATION").query_traffic(TARGET, q(10))
    engine = tape.replay(run, "ENGINE")
    with pytest.raises(ReplayDivergence):
        engine.query_traffic(TARGET, q(10))
    assert engine.for_caller("INVESTIGATION").query_traffic(TARGET, q(10)) == recorded
    assert engine.next_sequence is None  # callers share one cursor


def test_unsupported_capability_diverges_without_consuming(tape: Tape) -> None:
    run = tape.boundary("run-logs", capabilities=("logs",))
    window = (T0 + timedelta(minutes=10), T0 + timedelta(minutes=15))
    recorded = tape.live(run, "INVESTIGATION", ("logs",)).query_loki(
        ["payment-service"], *window, limit=8
    )
    replay = tape.replay(run, "INVESTIGATION")
    cursor_before = replay.next_sequence
    with pytest.raises(ReplayDivergence, match="not a provider capability"):
        replay.query_resource_pressure(TARGET, q(10))
    assert replay.next_sequence == cursor_before == 1
    assert replay.query_loki(["payment-service"], *window, limit=8) == recorded


def test_other_runs_tape_is_never_visible(tape: Tape) -> None:
    run_a = tape.boundary("run-a")
    run_b = tape.boundary("run-b")
    from_a = tape.live(run_a, "INVESTIGATION").query_traffic(TARGET, q(10))
    from_b = tape.live(run_b, "INVESTIGATION").query_traffic(TARGET, q(10))
    assert from_a != from_b
    replay_a = tape.replay(run_a, "INVESTIGATION")
    assert replay_a.query_traffic(TARGET, q(10)) == from_a
    with pytest.raises(ReplayDivergence):
        replay_a.query_traffic(TARGET, q(10))  # run B's identical read is not A's


def test_every_operation_and_key_form_replays_with_the_live_key(tape: Tape) -> None:
    run = tape.boundary("run-a")
    window = (T0 + timedelta(minutes=10), T0 + timedelta(minutes=15))
    tape.readers.tempo.batch = True
    calls: list[tuple[str, dict[str, Any]]] = [
        ("resource_pressure", {}),
        ("traffic", {"observation_identity": "obs:traffic:1"}),
        ("tempo", {"descriptor_id": "desc:tempo:1"}),
        ("loki", {}),
    ]

    def call(adapter: Any, name: str, kwargs: dict[str, Any]) -> Any:
        if name == "loki":
            return adapter.query_loki_with_read_id(["payment-service"], *window, limit=8)
        method = {
            "resource_pressure": adapter.query_resource_pressure,
            "traffic": adapter.query_traffic,
            "tempo": adapter.query_tempo,
        }[name]
        return method(TARGET, q(10), **kwargs)

    live = tape.live(run, "INVESTIGATION")
    recorded = [call(live, name, kwargs) for name, kwargs in calls]
    keys = [row[4] for row in tape.rows(run)]
    assert keys[1] == "obs:traffic:1" and keys[2] == "desc:tempo:1"
    assert len(keys[0]) == 64 and len(keys[3]) == 64  # canonical descriptor sha256
    replay = tape.replay(run, "INVESTIGATION")
    assert [call(replay, name, kwargs) for name, kwargs in calls] == recorded
    assert isinstance(recorded[2], TempoTraceBatch)
    assert recorded[3][1] == tape.rows(run)[3][0]  # Loki read id is the recorded row id


def test_replay_writes_nothing_and_calls_no_provider(
    tape: Tape, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tape.boundary("run-a")
    live = tape.live(run, "ENGINE")
    live.query_resource_pressure(TARGET, q(10))
    tape.readers.prometheus.fail = True
    live.query_resource_pressure(TARGET, q(11))
    before = tape.rows(run)
    tape.readers.go_offline()
    monkeypatch.setattr(ProviderAdapter, "_provider_call", _bomb("ProviderAdapter._provider_call"))
    monkeypatch.setattr(live_module, "urlopen", _bomb("urlopen"))
    replay = tape.replay(run, "ENGINE")
    replay.query_resource_pressure(TARGET, q(10))
    replay.query_resource_pressure(TARGET, q(11))
    with pytest.raises(ReplayDivergence):
        replay.query_resource_pressure(TARGET, q(12))
    assert tape.rows(run) == before


def test_capture_prefix_is_consumed_and_the_cursor_starts_after_it(tape: Tape) -> None:
    run = tape.boundary("run-a")
    window = (T0 + timedelta(minutes=10), T0 + timedelta(minutes=15))
    capture = tape.live(run, "CAPTURE")
    capture.query_loki(["payment-service"], *window)
    tape.readers.loki.fail = True
    capture.query_loki(["payment-service"], *window)  # an ERROR capture read is still prefix
    recorded = tape.live(run, "ENGINE").query_resource_pressure(TARGET, q(10))
    assert [row[2] for row in tape.rows(run)] == ["CAPTURE", "CAPTURE", "ENGINE"]
    replay = tape.replay(run, "ENGINE")
    assert replay.next_sequence == 3
    with pytest.raises(ReplayDivergence):  # capture is never replayed as a provider call
        replay.for_caller("CAPTURE").query_loki(["payment-service"], *window)
    assert replay.query_resource_pressure(TARGET, q(10)) == recorded


def test_capture_after_rca_reads_is_a_malformed_tape(tape: Tape) -> None:
    run = tape.boundary("run-a")
    window = (T0 + timedelta(minutes=10), T0 + timedelta(minutes=15))
    tape.live(run, "ENGINE").query_resource_pressure(TARGET, q(10))
    tape.live(run, "CAPTURE").query_loki(["payment-service"], *window)
    with pytest.raises(ReplayDataError, match="CAPTURE read after RCA reads"):
        tape.replay(run, "ENGINE")


def test_non_contiguous_sequences_are_a_malformed_tape(tape: Tape) -> None:
    run = tape.boundary("run-a")
    tape.live(run, "ENGINE").query_resource_pressure(TARGET, q(10))
    with tape.factory() as session:
        session.execute(
            update(InvestigationReadRow)
            .where(InvestigationReadRow.run_id == run)
            .values(sequence=2)
        )
        session.commit()
    with pytest.raises(ReplayDataError, match="not contiguous"):
        tape.replay(run, "ENGINE")


def test_undecodable_observation_fails_without_advancing(tape: Tape) -> None:
    run = tape.boundary("run-a")
    tape.live(run, "ENGINE").query_resource_pressure(TARGET, q(10))
    with tape.factory() as session:
        session.execute(
            update(InvestigationReadRow)
            .where(InvestigationReadRow.run_id == run)
            .values(observation=[{"not": "a resource pressure"}])
        )
        session.commit()
    replay = tape.replay(run, "ENGINE")
    with pytest.raises(ReplayDataError, match="cannot be decoded"):
        replay.query_resource_pressure(TARGET, q(10))
    assert replay.next_sequence == 1


# --- ReplaySource: engine and investigation paths over the tape -------------


def _bomb(name: str) -> Callable[..., Any]:
    def explode(*_: Any, **__: Any) -> Any:
        raise AssertionError(f"replay must not call {name}")

    return explode


def _live_run(
    world: Any, monkeypatch: pytest.MonkeyPatch, readers: ProviderReaders
) -> tuple[sessionmaker[Session], str, LiveSource]:
    factory, cluster, clock, incident_id = world
    cluster.objects.append(dict(POD))
    seen = Seen(monkeypatch)
    clock.now = WINDOW_END
    DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        provider_readers=readers,
        clock=clock,
    ).run(incident_id, "MANUAL")
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
    assert run_id is not None
    return factory, run_id, seen.source


def _chain(backend: Any) -> list[type[Any]]:
    kinds: list[type[Any]] = []
    while backend is not None:
        kinds.append(type(backend))
        backend = getattr(backend, "base", None)
    return kinds


def _fallback_bombs(monkeypatch: pytest.MonkeyPatch) -> None:
    for owner in (LiveSource, ReplaySource):
        for name in ("traffic_observations", "trace_observations"):
            monkeypatch.setattr(
                owner, name, _bomb(f"{owner.__name__}.{name}: provider replay fell through")
            )


def test_engine_and_investigation_reads_replay_from_tape_without_fallback(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readers = Readers()
    factory, run_id, live = _live_run(setup, monkeypatch, readers.configured())
    capture_rows = Tape(factory, None, readers).rows(run_id)
    assert {row[2] for row in capture_rows} <= {"CAPTURE"}
    # The live run's engine and investigation reads, in order.
    since = T0 + timedelta(minutes=12)
    engine_ok = live.resource_pressure([TARGET], since)
    readers.prometheus.fail = True
    engine_error = live.resource_pressure([TARGET], since)
    readers.prometheus.fail = False
    backend = live.investigation_backend()
    traffic = backend.query_traffic(TARGET, q(10))
    traces = backend.query_traces(WORKLOAD, q(10))
    logs = backend.query_logs(WORKLOAD, q(10))
    assert isinstance(engine_error, ProviderReadFailure)
    assert engine_ok and traffic and traces and logs
    before = Tape(factory, None, readers).rows(run_id)
    assert [row[2] for row in before[len(capture_rows) :]] == [
        "ENGINE",
        "ENGINE",
        "INVESTIGATION",
        "INVESTIGATION",
        "INVESTIGATION",
    ]

    readers.go_offline()
    _fallback_bombs(monkeypatch)
    monkeypatch.setattr(ProviderAdapter, "_provider_call", _bomb("ProviderAdapter._provider_call"))
    replay = ReplaySource.from_run(run_id, session_factory=factory)
    assert replay.resource_pressure([TARGET], since) == engine_ok
    assert replay.resource_pressure([TARGET], since) == engine_error
    replayed = replay.investigation_backend()
    assert _chain(replayed) == _chain(backend)
    assert replayed.query_traffic(TARGET, q(10)) == traffic
    assert replayed.query_traces(WORKLOAD, q(10)) == traces
    assert replayed.query_logs(WORKLOAD, q(10)) == logs
    assert replay.provider_adapter.next_sequence is None
    assert Tape(factory, None, readers).rows(run_id) == before


@pytest.mark.parametrize(
    "capabilities",
    [
        pytest.param((), id="none"),
        pytest.param(("logs",), id="loki"),
        pytest.param(("resource_pressure", "traffic"), id="prometheus"),
        pytest.param(("runtime_traces",), id="tempo"),
        pytest.param(PROVIDER_CAPABILITIES, id="all"),
    ],
)
def test_replay_builds_the_live_runs_backend_chain(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capabilities: tuple[str, ...],
) -> None:
    readers = Readers()
    factory, run_id, live = _live_run(setup, monkeypatch, readers.configured(capabilities))
    readers.go_offline()
    replay = ReplaySource.from_run(run_id, session_factory=factory)
    assert _chain(replay.investigation_backend()) == _chain(live.investigation_backend())
    kinds = set(_chain(replay.investigation_backend()))
    assert (LokiInvestigationBackend in kinds) == ("logs" in capabilities)
    assert (PrometheusInvestigationBackend in kinds) == ("traffic" in capabilities)
    assert (TempoInvestigationBackend in kinds) == ("runtime_traces" in capabilities)
    # Base persisted logs stay supported without Loki; Loki is only the typed provider.
    assert replay.supports("logs")
    assert replay.supports_typed_runtime("logs") == ("logs" in capabilities)
