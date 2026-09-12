"""Fake-provider investigation runtime tests with real bounded tool execution."""

import json
from datetime import UTC, datetime
from typing import Any

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
    InvestigationLimits,
    ReadOnlyToolRegistry,
    RegisteredTool,
    StopReason,
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


def registry(count: int = 1) -> ReadOnlyToolRegistry:
    """Build named read-only metric tools backed by deterministic data."""
    backend = metrics_tool(lambda _operation, _parameters: {"records": [{"p95": 1.7}]})
    return ReadOnlyToolRegistry(
        tuple(
            RegisteredTool(
                name="service_latency" if index == 0 else f"metric_{index}",
                version="1",
                operation="service_latency",
                source_type=EvidenceSourceType.METRIC,
                tool=backend,
            )
            for index in range(count)
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
    assert result.terminal_decision is DecisionType.SUBMIT_HYPOTHESIS
    assert result.usage.model_calls_limit == 3
    assert result.usage.model_budget_exhausted_after_terminal_decision is False
    assert len(audit.records) == 1
    assert audit.records[0].actual_api_calls == 0
    assert audit.records[0].estimated_api_calls == 2
    assert audit.records[0].terminal_decision is DecisionType.SUBMIT_HYPOTHESIS


def test_unknown_tool_fails_closed_without_backend_execution() -> None:
    """Unknown model tool names become a typed invalid decision result."""
    provider = FakeModelProvider(
        [{"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "kubectl"}]}]
    )

    result = InvestigationRuntime(provider, registry()).run(incident())

    assert result.termination_reason is TerminationReason.INVALID_DECISION
    assert result.error_code == "UNKNOWN_INVESTIGATION_TOOL"
    assert result.validation_stage is not None
    assert result.validation_stage.value == "TOOL_REGISTRY"
    assert result.validation_path == "$.requests[].tool"
    assert result.usage.tool_calls == 0


def test_empty_tool_requests_have_a_typed_semantic_failure() -> None:
    """A schema-valid empty batch remains invalid at the core boundary."""
    provider = FakeModelProvider(
        [{"decision": DecisionType.CALL_TOOLS, "requests": [], "hypothesis": None}]
    )

    result = InvestigationRuntime(provider, registry()).run(incident())

    assert result.termination_reason is TerminationReason.INVALID_DECISION
    assert result.error_code == "EMPTY_TOOL_REQUESTS"
    assert result.validation_stage is not None
    assert result.validation_stage.value == "DECISION_SCHEMA"


def test_unknown_hypothesis_mechanism_has_a_typed_semantic_failure() -> None:
    """The controlled hypothesis ontology remains enforced by the core."""
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.SUBMIT_HYPOTHESIS,
                "requests": [],
                "hypothesis": {
                    "affected_component": "payment-service",
                    "mechanism": "not-a-controlled-mechanism",
                    "suspected_trigger": "unknown",
                    "evidence_ids": ["00000000-0000-0000-0000-000000000001"],
                },
            }
        ]
    )

    result = InvestigationRuntime(provider, registry()).run(incident())

    assert result.termination_reason is TerminationReason.INVALID_DECISION
    assert result.error_code == "UNKNOWN_HYPOTHESIS_MECHANISM"


def test_model_budget_stops_after_three_turns() -> None:
    """A scripted agent cannot continue beyond the hard three-turn limit."""
    calls = [
        {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "service_latency"}]},
        {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "metric_1"}]},
        {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "metric_2"}]},
        {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "metric_3"}]},
    ]
    provider = FakeModelProvider(calls)

    result = InvestigationRuntime(provider, registry(4)).run(incident())

    assert result.termination_reason is TerminationReason.MODEL_CALL_LIMIT
    assert result.usage.model_calls == 3
    assert result.usage.tool_calls == 3
    assert result.terminal_decision is None


def test_final_allowed_call_can_submit_a_hypothesis() -> None:
    """A terminal hypothesis on call three is not relabeled as exhaustion."""

    def submit(model_request: ModelRequest) -> dict[str, Any]:
        context = json.loads(model_request.messages[1].content)
        return {
            "decision": DecisionType.SUBMIT_HYPOTHESIS,
            "hypothesis": {
                "affected_component": "payment-service",
                "mechanism": "service_latency_regression",
                "suspected_trigger": "elevated latency",
                "evidence_ids": [context["evidence"][0]["evidence_id"]],
            },
        }

    call_tools = {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "service_latency"}]}
    second_call = {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "metric_1"}]}
    result = InvestigationRuntime(
        FakeModelProvider([call_tools, second_call, submit]), registry(2)
    ).run(incident())

    assert result.termination_reason is TerminationReason.HYPOTHESIS_SUBMITTED
    assert result.terminal_decision is DecisionType.SUBMIT_HYPOTHESIS
    assert result.usage.model_calls == 3
    assert result.usage.model_budget_exhausted_after_terminal_decision is True


def test_final_allowed_call_can_stop_with_a_valid_reason() -> None:
    """A terminal STOP on call three keeps its actual termination semantics."""
    call_tools = {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "service_latency"}]}
    second_call = {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "metric_1"}]}
    stop = {
        "decision": DecisionType.STOP,
        "stop_reason": StopReason.INSUFFICIENT_EVIDENCE,
    }
    audit = InMemoryInvestigationAuditSink()
    result = InvestigationRuntime(
        FakeModelProvider([call_tools, second_call, stop]), registry(2), audit_sink=audit
    ).run(incident())

    assert result.termination_reason is TerminationReason.AGENT_STOPPED
    assert result.terminal_decision is DecisionType.STOP
    assert result.stop_reason is StopReason.INSUFFICIENT_EVIDENCE
    assert result.usage.model_calls == 3
    assert result.usage.model_budget_exhausted_after_terminal_decision is True
    assert audit.records[0].terminal_decision is DecisionType.STOP
    assert audit.records[0].stop_reason is StopReason.INSUFFICIENT_EVIDENCE
    assert audit.records[0].termination_reason is TerminationReason.AGENT_STOPPED


def test_final_model_request_exposes_only_terminal_decisions() -> None:
    """The runtime removes CALL_TOOLS before the final provider invocation."""
    call_tools = {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "service_latency"}]}
    second_call = {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "metric_1"}]}
    stop = {"decision": DecisionType.STOP, "stop_reason": StopReason.INSUFFICIENT_EVIDENCE}
    provider = FakeModelProvider([call_tools, second_call, stop])

    result = InvestigationRuntime(provider, registry(2)).run(incident())

    assert result.termination_reason is TerminationReason.AGENT_STOPPED
    assert provider.requests[0].allowed_decisions == (
        "CALL_TOOLS",
        "SUBMIT_HYPOTHESIS",
        "STOP",
    )
    assert provider.requests[1].allowed_decisions == provider.requests[0].allowed_decisions
    assert provider.requests[2].allowed_decisions == (
        "SUBMIT_HYPOTHESIS",
        "STOP",
    )


def test_reused_provider_records_per_run_accounting_delta() -> None:
    """A shared provider's cumulative counters do not leak into one run's usage."""
    stop = {"decision": DecisionType.STOP, "stop_reason": StopReason.INSUFFICIENT_EVIDENCE}
    provider = FakeModelProvider([stop, stop])

    first = InvestigationRuntime(provider, registry()).run(incident())
    second = InvestigationRuntime(provider, registry()).run(incident())

    assert first.usage.provider_invocations == 1
    assert second.usage.provider_invocations == 1
    assert first.usage.actual_api_calls == 0
    assert second.usage.actual_api_calls == 0


def test_provider_failure_on_final_call_is_not_model_exhaustion() -> None:
    """A provider failure on call three retains provider-error semantics."""

    class FailingFinalProvider(FakeModelProvider):
        def complete(self, request: ModelRequest):  # type: ignore[no-untyped-def]
            if len(self.requests) == 2:
                from packages.provider import ProviderError, ProviderErrorCode

                self.requests.append(request)
                raise ProviderError(ProviderErrorCode.PROVIDER_UNAVAILABLE, "offline fixture")
            return super().complete(request)

    call_tools = {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "service_latency"}]}
    second_call = {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "metric_1"}]}
    result = InvestigationRuntime(
        FailingFinalProvider([call_tools, second_call, call_tools]), registry(2)
    ).run(incident())

    assert result.termination_reason is TerminationReason.PROVIDER_ERROR
    assert result.terminal_decision is None
    assert result.usage.model_calls == 3


def test_decision_accepts_a_batch_up_to_the_incident_budget() -> None:
    """The provider-independent decision contract permits the full total budget."""
    decision = InvestigationDecision.model_validate(
        {
            "decision": DecisionType.CALL_TOOLS,
            "requests": [{"tool": f"tool-{index}"} for index in range(8)],
        }
    )

    assert len(decision.requests) == 8


def test_decision_rejects_more_than_the_incident_budget() -> None:
    """The contract still rejects a batch above the hard incident budget."""
    with pytest.raises(ValueError):
        InvestigationDecision.model_validate(
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [{"tool": f"tool-{index}"} for index in range(9)],
            }
        )


def _tool_requests(count: int) -> list[dict[str, str]]:
    """Build a batch of distinct registered tool requests."""
    return [
        {"tool": "service_latency" if index == 0 else f"metric_{index}"} for index in range(count)
    ]


@pytest.mark.parametrize(
    ("first_count", "second_count"),
    [(8, 0), (1, 7), (4, 4)],
)
def test_batches_use_remaining_total_tool_budget(first_count: int, second_count: int) -> None:
    """A batch may consume all currently remaining incident tool capacity."""
    responses: list[dict[str, Any]] = [
        {"decision": DecisionType.CALL_TOOLS, "requests": _tool_requests(first_count)}
    ]
    if second_count:
        responses.append(
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [
                    {
                        "tool": "service_latency" if index == 0 else f"metric_{index}",
                        "arguments": {"batch": "second"},
                    }
                    for index in range(second_count)
                ],
            }
        )
    responses.append(
        {"decision": DecisionType.STOP, "stop_reason": StopReason.INSUFFICIENT_EVIDENCE}
    )

    result = InvestigationRuntime(
        FakeModelProvider(responses),
        registry(8),
        limits=InvestigationLimits(max_agent_turns=2),
    ).run(incident())

    assert result.termination_reason is TerminationReason.AGENT_STOPPED
    assert result.error_code is None
    assert result.usage.tool_calls == 8
    assert len(result.evidence) == 8


def test_batch_above_remaining_budget_fails_closed() -> None:
    """A request above remaining capacity gets the total-budget failure."""
    provider = FakeModelProvider(
        [
            {"decision": DecisionType.CALL_TOOLS, "requests": _tool_requests(1)},
            {"decision": DecisionType.CALL_TOOLS, "requests": _tool_requests(8)},
        ]
    )

    result = InvestigationRuntime(provider, registry(8)).run(incident())

    assert result.termination_reason is TerminationReason.TOOL_CALL_LIMIT
    assert result.error_code == "TOOL_BUDGET_EXCEEDED"
    assert result.usage.tool_calls == 1


def test_batch_with_no_remaining_budget_fails_closed() -> None:
    """No tool request can execute once the total incident budget is consumed."""
    provider = FakeModelProvider(
        [
            {"decision": DecisionType.CALL_TOOLS, "requests": _tool_requests(8)},
            {"decision": DecisionType.CALL_TOOLS, "requests": [{"tool": "service_latency"}]},
        ]
    )

    result = InvestigationRuntime(provider, registry(8)).run(incident())

    assert result.termination_reason is TerminationReason.TOOL_CALL_LIMIT
    assert result.error_code == "TOOL_BUDGET_EXCEEDED"
    assert result.usage.tool_calls == 8


def test_exact_duplicate_tool_requests_are_rejected_without_execution() -> None:
    """Exact duplicate requests are not silently deduplicated or executed."""
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [
                    {"tool": "service_latency", "arguments": {"window": "5m"}},
                    {"tool": "service_latency", "arguments": {"window": "5m"}},
                ],
            }
        ]
    )

    result = InvestigationRuntime(provider, registry()).run(incident())

    assert result.termination_reason is TerminationReason.INVALID_DECISION
    assert result.error_code == "DUPLICATE_TOOL_REQUEST"
    assert result.usage.tool_calls == 0
    assert result.evidence == []
