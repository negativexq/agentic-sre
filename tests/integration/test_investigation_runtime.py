"""Fake-provider investigation runtime tests with real bounded tool execution."""

import json
from datetime import UTC, datetime

import pytest

from packages.contracts import (
    EvidenceSourceType,
    Incident,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.investigation import (
    DecisionType,
    InvestigationDecision,
    ReadOnlyToolRegistry,
    RegisteredTool,
    TerminationReason,
)
from packages.investigation.audit import InMemoryInvestigationAuditSink
from packages.investigation.runtime import InvestigationRuntime
from packages.provider import FakeModelProvider, ModelRequest
from packages.tools import metrics_tool


def incident() -> Incident:
    """Create a stable test incident."""
    now = datetime.now(UTC)
    return Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="Payment latency is elevated",
        created_at=now,
        updated_at=now,
    )


def registry() -> ReadOnlyToolRegistry:
    """Build one named read-only metric tool backed by deterministic data."""
    backend = metrics_tool(lambda _operation, _parameters: {"records": [{"p95": 1.7}]})
    return ReadOnlyToolRegistry(
        (
            RegisteredTool(
                name="service_latency",
                version="1",
                operation="service_latency",
                source_type=EvidenceSourceType.METRIC,
                tool=backend,
            ),
        )
    )


def test_fake_provider_runtime_batches_tools_and_submits_real_evidence() -> None:
    """The final hypothesis can cite only IDs created by the runtime."""

    def submit(model_request: ModelRequest) -> dict[str, object]:
        context = json.loads(model_request.messages[1].content)
        evidence_id = context["evidence"][0]["evidence_id"]
        return {
            "decision": DecisionType.SUBMIT_HYPOTHESIS,
            "hypothesis": {
                "affected_component": "payment-service",
                "mechanism": "service_latency_regression",
                "suspected_trigger": "elevated payment request latency",
                "evidence_ids": [evidence_id],
            },
        }

    provider = FakeModelProvider(
        [
            {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "service_latency"}]},
            submit,
        ]
    )
    audit = InMemoryInvestigationAuditSink()

    result = InvestigationRuntime(provider, registry(), audit_sink=audit).run(incident())

    assert result.termination_reason is TerminationReason.HYPOTHESIS_SUBMITTED
    assert result.hypothesis is not None
    assert len(result.evidence) == 1
    assert result.hypothesis.evidence_ids == [result.evidence[0].evidence_id]
    assert result.usage.model_calls == 2
    assert result.usage.tool_calls == 1
    assert len(audit.records) == 1
    assert audit.records[0].actual_api_calls == 0
    assert audit.records[0].estimated_api_calls == 2


def test_unknown_tool_fails_closed_without_backend_execution() -> None:
    """Unknown model tool names become a typed invalid decision result."""
    provider = FakeModelProvider(
        [{"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "kubectl"}]}]
    )

    result = InvestigationRuntime(provider, registry()).run(incident())

    assert result.termination_reason is TerminationReason.INVALID_DECISION
    assert result.error_code == "UNKNOWN_OR_FORBIDDEN_TOOL"
    assert result.usage.tool_calls == 0


def test_model_budget_stops_after_three_turns() -> None:
    """A scripted agent cannot continue beyond the hard three-turn limit."""
    call_tools = {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "service_latency"}]}
    provider = FakeModelProvider([call_tools, call_tools, call_tools, call_tools])

    result = InvestigationRuntime(provider, registry()).run(incident())

    assert result.termination_reason is TerminationReason.MODEL_CALL_LIMIT
    assert result.usage.model_calls == 3
    assert result.usage.tool_calls == 3


def test_decision_rejects_more_than_four_tools() -> None:
    """The structured contract rejects an oversized tool batch before runtime."""
    with pytest.raises(ValueError):
        InvestigationDecision.model_validate(
            {
                "decision": "CALL_TOOLS",
                "requests": [{"tool": f"tool-{index}"} for index in range(5)],
            }
        )
