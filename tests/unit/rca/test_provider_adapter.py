"""Single-boundary provider delegation and query identity contract."""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.rca.investigation.tempo import (
    TempoSearchCompleteness,
    TempoSearchDiagnostics,
    TempoTraceBatch,
)
from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    LogRecord,
    ResourcePressure,
    TrafficObservation,
)
from packages.rca.provider_adapter import (
    ProviderAdapter,
    ProviderCallerClass,
    ProviderReaders,
    provider_query_key,
)

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
        services: tuple[str, ...] | list[str],
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
        session_factory=lambda: None,
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


def test_adapter_carries_each_frozen_caller_class_without_using_storage() -> None:
    for caller_class in ("CAPTURE", "ENGINE", "INVESTIGATION"):
        adapter = _adapter(caller_class=caller_class)
        assert adapter.run_id == "run-1"
        assert adapter.caller_class == caller_class
        assert adapter.session_factory() is None


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


def test_repeated_identical_reads_are_not_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    query_keys: list[str] = []
    original_call = ProviderAdapter._provider_call

    def recording_call(self: ProviderAdapter, query_key: str, call: Callable[[], object]) -> object:
        query_keys.append(query_key)
        return original_call(self, query_key, call)

    monkeypatch.setattr(ProviderAdapter, "_provider_call", recording_call)

    response_a = adapter.query_resource_pressure(TARGET, QUERY)
    response_b = adapter.query_resource_pressure(TARGET, QUERY)

    assert len(reader.pressure_calls) == 2
    assert response_a is first and response_b is second
    assert response_a != response_b
    assert len(query_keys) == 2 and query_keys[0] == query_keys[1]


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
