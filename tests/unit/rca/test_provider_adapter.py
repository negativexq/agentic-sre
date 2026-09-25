"""Single-boundary provider delegation and query identity contract."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from provider_test_helpers import provider_session_factory
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.rca.investigation.tempo import (
    TempoSearchCompleteness,
    TempoSearchDiagnostics,
    TempoTraceBatch,
)
from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    LogRecord,
    ProviderReadFailure,
    ResourcePressure,
    TrafficObservation,
)
from packages.rca.provider_adapter import (
    ProviderAdapter,
    ProviderCallerClass,
    ProviderReaders,
    provider_query_key,
)
from packages.storage.models import InvestigationReadRow

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
TARGET = EntityRef(namespace="sre-demo", kind="Pod", name="worker")
QUERY = InvestigationQuery(start=T0, end=T0, limit=2)


class _Prometheus:
    def __init__(self) -> None:
        self.pressure_calls: list[tuple[EntityRef, InvestigationQuery]] = []
        self.traffic_calls: list[tuple[EntityRef, InvestigationQuery]] = []
        self.pressure_results: list[tuple[ResourcePressure, ...]] = []
        self.traffic_result: tuple[TrafficObservation, ...] = ()

    def query_resource_pressure(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[ResourcePressure, ...]:
        self.pressure_calls.append((target, query))
        return self.pressure_results.pop(0)

    def query_traffic(
        self, target: EntityRef, query: InvestigationQuery
    ) -> tuple[TrafficObservation, ...]:
        self.traffic_calls.append((target, query))
        return self.traffic_result


class _Loki:
    def __init__(self, result: list[LogRecord]) -> None:
        self.calls: list[tuple[tuple[str, ...], datetime, datetime, int | None]] = []
        self.result = result

    def error_logs(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> list[LogRecord]:
        self.calls.append((tuple(services), starts_at, ends_at, limit))
        return self.result


class _Tempo:
    def __init__(self, result: tuple[object, ...]) -> None:
        self.calls: list[tuple[EntityRef, InvestigationQuery]] = []
        self.result = result

    def query(self, target: EntityRef, query: InvestigationQuery) -> tuple[object, ...]:
        self.calls.append((target, query))
        return self.result


def _adapter(
    *,
    caller_class: ProviderCallerClass = "INVESTIGATION",
    prometheus: object | None = None,
    loki: object | None = None,
    tempo: object | None = None,
) -> ProviderAdapter:
    return ProviderAdapter(
        run_id="run-1",
        caller_class=caller_class,
        session_factory=provider_session_factory(),
        readers=ProviderReaders(
            prometheus=prometheus,  # type: ignore[arg-type]
            loki=loki,  # type: ignore[arg-type]
            tempo=tempo,  # type: ignore[arg-type]
        ),
    )


def test_prometheus_operations_delegate_once_and_return_the_same_values() -> None:
    reader = _Prometheus()
    pressure = ResourcePressure(
        pod=TARGET,
        container="app",
        resource="cpu",
        baseline=0.1,
        peak=0.8,
        at=T0,
        evidence_id="pressure-1",
    )
    pressure_result = (pressure,)
    traffic_result = (
        TrafficObservation(
            entity=TARGET,
            metric="http_requests_per_second",
            at=T0,
            value=2.0,
            evidence_id="traffic-1",
        ),
    )
    reader.pressure_results.append(pressure_result)
    reader.traffic_result = traffic_result
    adapter = _adapter(prometheus=reader)

    assert adapter.query_resource_pressure(TARGET, QUERY) is pressure_result
    assert adapter.query_traffic(TARGET, QUERY) is traffic_result
    assert reader.pressure_calls == [(TARGET, QUERY)]
    assert reader.traffic_calls == [(TARGET, QUERY)]


def test_loki_delegates_once_and_returns_the_same_records() -> None:
    result = [LogRecord(service="worker", at=T0, severity="ERROR", message="x", evidence_id="l1")]
    reader = _Loki(result)
    adapter = _adapter(caller_class="CAPTURE", loki=reader)

    returned = adapter.error_logs(["worker"], T0, T0, limit=8)

    assert returned is result
    assert reader.calls == [(("worker",), T0, T0, 8)]


def test_tempo_delegates_once_and_returns_the_same_batch() -> None:
    batch = TempoTraceBatch(
        spans=(),
        diagnostics=TempoSearchDiagnostics(
            completeness=TempoSearchCompleteness.BEST_EFFORT,
            candidate_trace_ids=(),
            search_limit_reached=False,
            inspected_traces=0,
            inspected_bytes=0,
            completed_jobs=0,
            total_jobs=0,
            fetched_trace_ids=(),
            missing_trace_ids=(),
        ),
    )
    reader = _Tempo(batch)  # type: ignore[arg-type]
    adapter = _adapter(tempo=reader)

    assert adapter.query_tempo(TARGET, QUERY) is batch
    assert reader.calls == [(TARGET, QUERY)]


def test_adapter_carries_each_frozen_caller_class() -> None:
    for caller_class in ("CAPTURE", "ENGINE", "INVESTIGATION"):
        adapter = _adapter(caller_class=caller_class)
        assert adapter.run_id == "run-1"
        assert adapter.caller_class == caller_class
        assert callable(adapter.session_factory)


def test_successful_call_persists_a_complete_normalized_success_envelope() -> None:
    reader = _Prometheus()
    pressure = ResourcePressure(
        pod=TARGET,
        container="app",
        resource="cpu",
        baseline=0.1,
        peak=0.8,
        at=T0,
        evidence_id="pressure-success",
    )
    result = (pressure,)
    reader.pressure_results.append(result)
    adapter = _adapter(caller_class="ENGINE", prometheus=reader)

    assert adapter.query_resource_pressure(TARGET, QUERY) is result
    with adapter.session_factory() as session:
        row = session.scalar(select(InvestigationReadRow))
    assert row is not None
    assert row.run_id == "run-1"
    assert row.sequence == 1
    assert row.caller_class == "ENGINE"
    assert row.capability == "resource_pressure"
    assert row.query_key == provider_query_key(
        {
            "provider": "prometheus",
            "operation": "resource_pressure",
            "target": TARGET.canonical,
            "query": QUERY.model_dump(mode="json"),
        }
    )
    assert row.query_descriptor == {
        "provider": "prometheus",
        "operation": "resource_pressure",
        "target": TARGET.canonical,
        "query": QUERY.model_dump(mode="json"),
    }
    assert row.started_at is not None and row.finished_at is not None
    assert row.committed_at is not None
    assert (
        row.started_at is not None and row.finished_at is not None and row.committed_at is not None
    )
    started_at, finished_at, committed_at = row.started_at, row.finished_at, row.committed_at
    assert started_at is not None and finished_at is not None and committed_at is not None
    assert started_at <= finished_at <= committed_at
    assert row.status == "SUCCESS"
    assert row.observation == [pressure.model_dump(mode="json")]
    assert row.evidence_ids == ["pressure-success"]
    assert row.error_type is None and row.error_message is None


def test_each_frozen_caller_class_is_persisted_without_inference() -> None:
    factory = provider_session_factory()
    pressure = ResourcePressure(
        pod=TARGET,
        container="app",
        resource="cpu",
        baseline=0.1,
        peak=0.8,
        at=T0,
        evidence_id="pressure",
    )
    prom = _Prometheus()
    prom.pressure_results.extend(((pressure,), (pressure,)))
    loki = _Loki(
        [LogRecord(service="worker", at=T0, severity="ERROR", message="x", evidence_id="log")]
    )
    base = ProviderAdapter(
        "caller-run",
        "ENGINE",
        factory,
        ProviderReaders(prometheus=prom, loki=loki),
    )

    base.for_caller("ENGINE").query_resource_pressure(TARGET, QUERY)
    base.for_caller("INVESTIGATION").query_resource_pressure(TARGET, QUERY)
    base.for_caller("CAPTURE").query_loki(["worker"], T0, T0)

    with factory() as session:
        rows = list(
            session.scalars(select(InvestigationReadRow).order_by(InvestigationReadRow.sequence))
        )
    assert [(row.caller_class, row.capability) for row in rows] == [
        ("ENGINE", "resource_pressure"),
        ("INVESTIGATION", "resource_pressure"),
        ("CAPTURE", "loki_logs"),
    ]


def test_sequence_counters_are_isolated_by_run_id() -> None:
    factory = provider_session_factory()
    result = (
        ResourcePressure(
            pod=TARGET,
            container="app",
            resource="cpu",
            baseline=0.1,
            peak=0.8,
            at=T0,
            evidence_id="pressure",
        ),
    )
    first_reader = _Prometheus()
    first_reader.pressure_results.extend((result, result))
    second_reader = _Prometheus()
    second_reader.pressure_results.append(result)
    ProviderAdapter(
        "run-a", "ENGINE", factory, ProviderReaders(prometheus=first_reader)
    ).query_resource_pressure(TARGET, QUERY)
    ProviderAdapter(
        "run-a", "ENGINE", factory, ProviderReaders(prometheus=first_reader)
    ).query_resource_pressure(TARGET, QUERY)
    ProviderAdapter(
        "run-b", "ENGINE", factory, ProviderReaders(prometheus=second_reader)
    ).query_resource_pressure(TARGET, QUERY)

    with factory() as session:
        rows = list(
            session.scalars(
                select(InvestigationReadRow).order_by(
                    InvestigationReadRow.run_id, InvestigationReadRow.sequence
                )
            )
        )
    assert [(row.run_id, row.sequence) for row in rows] == [
        ("run-a", 1),
        ("run-a", 2),
        ("run-b", 1),
    ]


def test_persistence_failure_prevents_successful_result_from_escaping() -> None:
    reader = _Prometheus()
    successful_result = (
        ResourcePressure(
            pod=TARGET,
            container="app",
            resource="cpu",
            baseline=0.1,
            peak=0.8,
            at=T0,
            evidence_id="must-not-escape",
        ),
    )
    reader.pressure_results.append(successful_result)

    def failing_session_factory() -> Session:
        raise RuntimeError("storage unavailable")

    adapter = ProviderAdapter(
        "run-failure",
        "ENGINE",
        failing_session_factory,
        ProviderReaders(prometheus=reader),
    )
    consumed: list[object] = []

    with pytest.raises(RuntimeError, match="could not be persisted"):
        consumed.append(adapter.query_resource_pressure(TARGET, QUERY))
    assert len(reader.pressure_calls) == 1
    assert consumed == []


def test_provider_failure_is_taped_as_error_and_distinct_from_empty_success() -> None:
    class RaisingPrometheus(_Prometheus):
        def query_resource_pressure(
            self, target: EntityRef, query: InvestigationQuery
        ) -> tuple[ResourcePressure, ...]:
            self.pressure_calls.append((target, query))
            raise ConnectionError("Bearer abc123 secret=sk-super-secret")

    factory = provider_session_factory()
    adapter = ProviderAdapter(
        "error-run", "ENGINE", factory, ProviderReaders(prometheus=RaisingPrometheus())
    )
    long_identity = "observation-identity-" + ("x" * 398)
    assert len(long_identity) == 419
    result = adapter.query_resource_pressure(TARGET, QUERY, observation_identity=long_identity)
    assert isinstance(result, ProviderReadFailure)
    assert result.error_type == "ConnectionError"
    assert "[REDACTED]" in result.error_message
    assert "abc123" not in result.error_message
    assert "sk-super-secret" not in result.error_message

    empty_reader = _Prometheus()
    empty_reader.pressure_results.append(())
    empty = _adapter(prometheus=empty_reader).query_resource_pressure(TARGET, QUERY)
    assert empty == ()
    assert not isinstance(empty, ProviderReadFailure)
    with factory() as session:
        row = session.scalar(select(InvestigationReadRow))
    assert row is not None
    assert row.status == "ERROR"
    assert row.caller_class == "ENGINE"
    assert row.capability == "resource_pressure"
    assert row.query_key == long_identity
    assert row.query_descriptor["operation"] == "resource_pressure"
    assert row.error_type == "ConnectionError"
    assert row.error_message == result.error_message
    assert row.evidence_ids == []
    assert row.observation is None
    started_at = row.started_at
    finished_at = row.finished_at
    committed_at = row.committed_at
    assert started_at is not None and finished_at is not None and committed_at is not None
    assert started_at <= finished_at <= committed_at


def test_error_persistence_failure_does_not_return_typed_missing() -> None:
    class RaisingPrometheus(_Prometheus):
        def query_resource_pressure(
            self, target: EntityRef, query: InvestigationQuery
        ) -> tuple[ResourcePressure, ...]:
            raise ConnectionError("provider unavailable")

    def failing_factory() -> Session:
        raise RuntimeError("storage unavailable")

    adapter = ProviderAdapter(
        "error-persist-fails",
        "ENGINE",
        failing_factory,
        ProviderReaders(prometheus=RaisingPrometheus()),
    )
    consumed: list[object] = []
    with pytest.raises(RuntimeError, match="failed resource_pressure read could not be persisted"):
        consumed.append(adapter.query_resource_pressure(TARGET, QUERY))
    assert consumed == []


def test_repeated_provider_failures_keep_query_identity_and_advance_sequence() -> None:
    class IntermittentPrometheus(_Prometheus):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def query_resource_pressure(
            self, target: EntityRef, query: InvestigationQuery
        ) -> tuple[ResourcePressure, ...]:
            self.calls += 1
            if self.calls in {2, 4, 5}:
                raise TimeoutError("timeout")
            return ()

    factory = provider_session_factory()
    reader = IntermittentPrometheus()
    adapter = ProviderAdapter(
        "mixed-run", "INVESTIGATION", factory, ProviderReaders(prometheus=reader)
    )
    first = adapter.query_resource_pressure(TARGET, QUERY)
    failed_a = adapter.query_resource_pressure(TARGET, QUERY, observation_identity="same-query")
    second_success = adapter.query_resource_pressure(
        TARGET, QUERY, observation_identity="same-query"
    )
    failed_b = adapter.query_resource_pressure(TARGET, QUERY, observation_identity="same-query")
    failed_c = adapter.query_resource_pressure(TARGET, QUERY, observation_identity="same-query")
    assert first == ()
    assert second_success == ()
    assert isinstance(failed_a, ProviderReadFailure)
    assert isinstance(failed_b, ProviderReadFailure)
    assert isinstance(failed_c, ProviderReadFailure)
    with factory() as session:
        rows = list(
            session.scalars(select(InvestigationReadRow).order_by(InvestigationReadRow.sequence))
        )
    assert [row.sequence for row in rows] == [1, 2, 3, 4, 5]
    assert [row.status for row in rows] == ["SUCCESS", "ERROR", "SUCCESS", "ERROR", "ERROR"]
    assert rows[1].query_key == rows[3].query_key == rows[4].query_key == "same-query"


def test_query_key_precedence_uses_descriptor_then_observation_identity() -> None:
    descriptor = {"query": "ignored when an identity exists"}

    assert (
        provider_query_key(
            descriptor,
            descriptor_id="descriptor-id",
            observation_identity="observation-id",
        )
        == "descriptor-id"
    )
    assert provider_query_key(descriptor, observation_identity="observation-id") == "observation-id"


def test_canonical_query_fallback_is_stable_and_sensitive_to_descriptor_changes() -> None:
    first = {"target": "sre-demo/Pod/worker", "limit": 8, "range": {"end": "12", "start": "10"}}
    equivalent = {
        "range": {"start": "10", "end": "12"},
        "limit": 8,
        "target": "sre-demo/Pod/worker",
    }
    changed = {**first, "limit": 9}

    first_key = provider_query_key(first)
    assert first_key == provider_query_key(equivalent)
    assert first_key != provider_query_key(changed)


def test_repeated_identical_reads_are_not_deduplicated() -> None:
    reader = _Prometheus()
    first = (
        ResourcePressure(
            pod=TARGET,
            container="app",
            resource="cpu",
            baseline=0.1,
            peak=0.2,
            at=T0,
            evidence_id="pressure-1",
        ),
    )
    second = (
        ResourcePressure(
            pod=TARGET,
            container="app",
            resource="cpu",
            baseline=0.1,
            peak=0.3,
            at=T0,
            evidence_id="pressure-2",
        ),
    )
    reader.pressure_results.extend((first, second))
    adapter = _adapter(caller_class="ENGINE", prometheus=reader)
    response_a = adapter.query_resource_pressure(TARGET, QUERY)
    response_b = adapter.query_resource_pressure(TARGET, QUERY)

    assert len(reader.pressure_calls) == 2
    assert response_a is first and response_b is second
    assert response_a != response_b
    with adapter.session_factory() as session:
        rows = list(
            session.scalars(select(InvestigationReadRow).order_by(InvestigationReadRow.sequence))
        )
    assert [row.sequence for row in rows] == [1, 2]
    assert rows[0].query_key == rows[1].query_key
    assert rows[0].evidence_ids == ["pressure-1"]
    assert rows[1].evidence_ids == ["pressure-2"]


def test_production_reader_names_are_confined_to_the_provider_boundary() -> None:
    repository = Path(__file__).resolve().parents[3]
    reader_names = {"PrometheusMetricsReader", "LokiLogReader", "TempoTraceReader"}
    allowed_definitions = {
        "packages/rca/live.py": {"LokiLogReader"},
        "packages/rca/investigation/prometheus.py": {"PrometheusMetricsReader"},
        "packages/rca/investigation/tempo.py": {"TempoTraceReader"},
    }
    violations: list[str] = []

    for root in (repository / "apps", repository / "packages", repository / "scripts"):
        for path in root.rglob("*.py"):
            relative = path.relative_to(repository).as_posix()
            if relative == "packages/rca/provider_adapter.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            allowed = allowed_definitions.get(relative, set())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name in reader_names:
                            violations.append(f"{relative}:{node.lineno}: imports {alias.name}")
                elif isinstance(node, ast.Name) and node.id in reader_names:
                    if node.id not in allowed:
                        violations.append(f"{relative}:{node.lineno}: references {node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in reader_names:
                    violations.append(f"{relative}:{node.lineno}: accesses {node.attr}")

    assert violations == []
