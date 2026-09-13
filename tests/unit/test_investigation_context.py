"""Offline tests for bounded model-facing investigation context."""

import json
from datetime import UTC, datetime
from uuid import uuid4

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
from packages.investigation.context import CompactContextBuilder
from packages.investigation.topology import DEFAULT_TOPOLOGY


def _incident() -> Incident:
    now = datetime.now(UTC)
    return Incident(
        incident_id=uuid4(),
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="KafkaConsumerLag",
        created_at=now,
        updated_at=now,
    )


def _alert() -> Alert:
    now = datetime.now(UTC)
    return Alert(
        alert_name="KafkaConsumerLag",
        service="order-worker",
        namespace="sre-demo",
        cluster="agentic-sre",
        starts_at=now,
        labels={"severity": "critical", "service": "order-worker"},
        annotations={"description": "consumer lag is elevated"},
        fingerprint="fingerprint",
        status=AlertStatus.FIRING,
        source=AlertSource.PROMETHEUS,
    )


def test_context_exposes_alert_catalog_progress_and_future_turns() -> None:
    """Context makes current and future budget semantics explicit."""
    builder = CompactContextBuilder()
    descriptor = {
        "name": "kafka_consumer_lag",
        "version": "1",
        "purpose": "Measure bounded Kafka lag",
        "evidence_type": EvidenceSourceType.METRIC.value,
        "arguments": {"consumer": {"type": "string", "required": True}},
    }
    for current, future, final in ((1, 2, False), (2, 1, False), (3, 0, True)):
        context = json.loads(
            builder.build(
                _incident(),
                [],
                (descriptor,),
                alerts=(_alert(),),
                progress=(
                    {
                        "tool": "kafka_consumer_lag",
                        "arguments": {"consumer": "order-worker"},
                        "status": "SUCCESS",
                        "evidence_ids": [],
                    },
                ),
                current_model_call=current,
                max_model_calls=3,
                tool_calls_used=1,
                tool_calls_remaining=7,
            )
        )
        assert context["execution"]["current_model_call"] == current
        assert context["execution"]["future_model_calls_after_this_decision"] == future
        assert context["execution"]["is_final_model_turn"] is final
        assert context["alerts"][0]["service"] == "order-worker"
        assert context["tool_catalog"][0]["arguments"]["consumer"]["required"] is True
        assert context["progress"][0]["status"] == "SUCCESS"


def test_a1_context_exposes_topology_and_separate_alert_scope() -> None:
    """A1 receives production topology without causal-answer metadata."""
    context = json.loads(
        CompactContextBuilder().build(
            _incident(),
            [],
            (),
            alerts=(_alert(),),
            topology=DEFAULT_TOPOLOGY,
            current_model_call=1,
            max_model_calls=5,
            tool_calls_remaining=12,
        )
    )

    assert context["topology"] == DEFAULT_TOPOLOGY.serialize()
    assert context["investigation_state"] == {
        "alert_scope": "order-worker",
        "queried_workloads": [],
        "queried_resources": [],
    }
    context_text = json.dumps(context)
    assert "expected_causal_component" not in context_text
    assert "ground_truth" not in context_text


def test_a1_state_includes_validated_failed_and_reused_targets() -> None:
    """Query state reflects tool activity, not only successful evidence."""
    context = json.loads(
        CompactContextBuilder().build(
            _incident(),
            [],
            (),
            alerts=(_alert(),),
            topology=DEFAULT_TOPOLOGY,
            progress=(
                {
                    "tool": "service_latency",
                    "arguments": {"service": "payment-service"},
                    "status": "BACKEND_UNAVAILABLE",
                },
            ),
        )
    )

    assert context["investigation_state"]["queried_workloads"] == ["payment-service"]
