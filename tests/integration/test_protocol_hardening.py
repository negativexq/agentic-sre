"""Offline protocol, contract, and temporal provenance regression tests."""

from datetime import UTC, datetime, timedelta

from packages.contracts import (
    Alert,
    AlertSource,
    AlertStatus,
    EvidenceSourceType,
    Incident,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.investigation import InvestigationRuntime, ReadOnlyToolRegistry, RegisteredTool
from packages.investigation.context import derive_observation_window
from packages.investigation.tool_contracts import ConsumerArgs, ServiceArgs
from packages.provider import FakeModelProvider
from packages.tools import logs_tool, metrics_tool


def _incident(now: datetime) -> Incident:
    return Incident(
        status=IncidentStatus.RESOLVED,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="KafkaConsumerLag",
        created_at=now,
        updated_at=now + timedelta(seconds=30),
    )


def _alert(now: datetime) -> Alert:
    return Alert(
        alert_name="KafkaConsumerLag",
        service="order-worker",
        namespace="sre-demo",
        cluster="kind-agentic-sre",
        starts_at=now,
        ends_at=now + timedelta(seconds=30),
        labels={"severity": "CRITICAL"},
        annotations={"description": "consumer lag"},
        fingerprint="kafka-fixture",
        status=AlertStatus.RESOLVED,
        source=AlertSource.ALERTMANAGER,
    )


def test_resolved_alert_derives_incident_window_not_current_time() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    window = derive_observation_window(
        _incident(start), (_alert(start),), now=start + timedelta(days=1)
    )
    assert window.source == "RESOLVED_ALERT"
    assert window.starts_at == start
    assert window.ends_at == start + timedelta(seconds=30)


def test_resolved_kafka_fixture_creates_incident_time_evidence() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    observed: list[dict[str, object]] = []

    def backend(_operation: str, parameters: dict[str, object]) -> dict[str, object]:
        observed.append(parameters)
        window = parameters["observation_window"]
        assert isinstance(window, dict)
        return {
            "records": [{"lag": 900, "window": window}],
            "__effective_time_window": window,
            "__temporal_mode": "INCIDENT_WINDOW",
        }

    metric_tool = metrics_tool(backend)
    log_tool = logs_tool(backend)
    registry = ReadOnlyToolRegistry(
        (
            RegisteredTool(
                "kafka_consumer_lag",
                "1",
                "kafka_consumer_lag",
                EvidenceSourceType.METRIC,
                metric_tool,
                argument_model=ConsumerArgs,
            ),
            RegisteredTool(
                "service_logs",
                "1",
                "query_logs",
                EvidenceSourceType.LOG,
                log_tool,
                argument_model=ServiceArgs,
            ),
        )
    )
    provider = FakeModelProvider(
        [
            {
                "decision": "CALL_TOOLS",
                "requests": [
                    {"tool": "kafka_consumer_lag", "arguments": {"consumer": "order-worker"}},
                    {"tool": "service_logs", "arguments": {"service": "order-worker"}},
                ],
            },
            {"decision": "STOP", "stop_reason": "insufficient_evidence"},
        ]
    )
    result = InvestigationRuntime(provider, registry).run(_incident(start), alerts=(_alert(start),))
    assert result.error_code is None
    assert len(result.evidence) == 2
    assert all(item.time_window.starts_at == start for item in result.evidence)
    assert all(
        item.time_window.ends_at == start + timedelta(seconds=30) for item in result.evidence
    )
    assert len(observed) == 2


def test_all_live_tools_use_canonical_argument_models() -> None:
    from packages.investigation.registry import live_observability_registry

    registry = live_observability_registry("http://prometheus", "http://loki", "http://tempo")
    assert len(registry.names()) == 17
    for name in registry.names():
        tool = registry.get(name)
        assert tool.descriptor()["arguments"] is not None
        fields = tool.argument_model.model_fields
        required = {key for key, field in fields.items() if field.is_required()}
        values = {
            "service": "payment-service",
            "consumer": "order-worker",
            "deployment": "payment-service",
            "trace_id": "0" * 32,
        }
        canonical = registry.validate(name, {key: values[key] for key in required})
        assert set(canonical) == required
        for key in required:
            try:
                registry.validate(name, {})
            except ValueError as error:
                assert "tool arguments" in str(error)
            else:  # pragma: no cover - defensive assertion
                raise AssertionError(f"{name} accepted missing {key}")
