"""Fake-provider investigation runtime tests with real bounded tool execution."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

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
from packages.investigation import (
    A1InvestigationDecision,
    A1RunArtifact,
    DecisionType,
    InvestigationDecision,
    InvestigationLimits,
    ReadOnlyToolRegistry,
    RegisteredTool,
    StopReason,
    TerminationReason,
    ToolRepeatPolicy,
)
from packages.investigation.audit import InMemoryInvestigationAuditSink
from packages.investigation.causal_contracts import EvidenceCategory, TriggerType
from packages.investigation.context import derive_observation_window
from packages.investigation.prompt import investigator_prompt_v4_hash
from packages.investigation.runtime import InvestigationRuntime
from packages.investigation.tool_contracts import ServiceArgs
from packages.investigation.topology import DEFAULT_TOPOLOGY, WorkloadComponentId
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


def latency_alert() -> Alert:
    """Create an order-service symptom alert for A1 topology tests."""
    now = datetime.now(UTC)
    return Alert(
        alert_name="OrderDependencyLatencyHigh",
        service="order-service",
        namespace="sre-demo",
        cluster="agentic-sre",
        starts_at=now,
        labels={"severity": "critical", "service": "order-service"},
        annotations={"description": "dependency latency is elevated"},
        fingerprint="a1-topology-test",
        status=AlertStatus.FIRING,
        source=AlertSource.PROMETHEUS,
    )


def registry(
    count: int = 1,
    repeat_policy: ToolRepeatPolicy = ToolRepeatPolicy.FIXED_WINDOW,
) -> ReadOnlyToolRegistry:
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
                repeat_policy=repeat_policy,
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
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [{"tool": "service_latency", "arguments": {"service": "order-worker"}}],
            },
            submit,
        ]
    )
    audit = InMemoryInvestigationAuditSink()

    result = InvestigationRuntime(provider, registry(), audit_sink=audit).run(incident())

    assert result.termination_reason is TerminationReason.HYPOTHESIS_SUBMITTED
    assert result.hypothesis is not None
    assert len(result.evidence) == 1
    assert result.hypothesis.evidence_ids == [result.evidence[0].evidence_id]
    assert result.evidence[0].target_workload == "order-worker"
    assert result.evidence[0].target_resource is None
    assert result.usage.model_calls == 2
    assert result.usage.tool_calls == 1
    assert result.terminal_decision is DecisionType.SUBMIT_HYPOTHESIS
    assert result.usage.model_calls_limit == 3
    assert result.usage.model_budget_exhausted_after_terminal_decision is False
    assert len(audit.records) == 1
    assert audit.records[0].actual_api_calls == 0
    assert audit.records[0].estimated_api_calls == 2
    assert audit.records[0].terminal_decision is DecisionType.SUBMIT_HYPOTHESIS


def test_a1_protocol_exposes_topology_and_supports_cross_component_hypothesis() -> None:
    """A1 can inspect a dependency and return the structured causal result."""

    def submit(model_request: ModelRequest) -> dict[str, object]:
        context = json.loads(model_request.messages[1].content)
        evidence_ids = [item["evidence_id"] for item in context["evidence"]]
        return {
            "decision": DecisionType.SUBMIT_HYPOTHESIS,
            "hypothesis": {
                "symptom_component": WorkloadComponentId.ORDER_SERVICE,
                "causal_component": WorkloadComponentId.PAYMENT_SERVICE,
                "causal_resource": None,
                "mechanism": "dependency_latency",
                "structured_trigger": {
                    "trigger_type": TriggerType.DEPENDENCY_LATENCY_INCREASE,
                    "trigger_component": WorkloadComponentId.PAYMENT_SERVICE,
                    "trigger_resource": None,
                },
                "causal_summary": "payment dependency latency explains the order symptom",
                "evidence_ids": evidence_ids,
            },
        }

    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [
                    {"tool": "service_latency", "arguments": {"service": "order-service"}}
                ],
            },
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [
                    {"tool": "service_latency", "arguments": {"service": "payment-service"}}
                ],
            },
            submit,
        ]
    )

    result = InvestigationRuntime(provider, registry(), a1_protocol=True).run(
        incident(), (latency_alert(),)
    )

    assert result.termination_reason is TerminationReason.HYPOTHESIS_SUBMITTED
    assert result.causal_hypothesis is not None
    assert result.causal_hypothesis["causal_component"] == "payment-service"
    assert result.hypothesis is not None
    assert result.usage.prompt_hash == investigator_prompt_v4_hash()
    assert result.usage.tool_calls == 2
    assert result.usage.duplicate_requests_suppressed == 0
    assert len(provider.requests) == 3
    assert provider.requests[0].response_schema_name == "a1_investigation_decision"
    first_context = json.loads(provider.requests[0].messages[1].content)
    assert first_context["investigation_state"]["alert_scope"] == "order-service"
    assert first_context["topology"]["dependencies"]
    provider_payload = json.dumps(provider.requests[0].model_dump(mode="json"))
    for evaluator_value in (
        "V020-003",
        "payment_dependency_latency",
        "expected_causal_component",
        "grader_support_predicate",
    ):
        assert evaluator_value not in provider_payload
    second_context = json.loads(provider.requests[1].messages[1].content)
    assert second_context["investigation_state"]["queried_workloads"] == ["order-service"]
    terminal_context = json.loads(provider.requests[2].messages[1].content)
    assert {item["target_workload"] for item in terminal_context["evidence"]} == {
        "order-service",
        "payment-service",
    }
    artifact = A1RunArtifact.from_result(
        result,
        experiment_id="a1-offline",
        observation_window=derive_observation_window(incident(), (latency_alert(),)).time_window(),
    )
    assert artifact.hypothesis is not None
    assert artifact.hypothesis.causal_component is WorkloadComponentId.PAYMENT_SERVICE
    assert artifact.evidence[0].target_workload is WorkloadComponentId.ORDER_SERVICE


def test_a1_protocol_persists_structured_stop_metadata() -> None:
    """STOP remains valid and its bounded audit fields are retained."""
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.STOP,
                "stop": {
                    "stop_reason": StopReason.INSUFFICIENT_EVIDENCE,
                    "considered_components": [WorkloadComponentId.ORDER_SERVICE],
                    "considered_resources": [],
                    "missing_evidence_categories": [EvidenceCategory.TRACES],
                },
            }
        ]
    )

    result = InvestigationRuntime(provider, registry(), a1_protocol=True).run(
        incident(), (latency_alert(),)
    )

    assert result.termination_reason is TerminationReason.AGENT_STOPPED
    assert result.causal_stop == {
        "stop_reason": "insufficient_evidence",
        "considered_components": ["order-service"],
        "considered_resources": [],
        "missing_evidence_categories": ["TRACES"],
    }


def test_a1_protocol_supports_resource_precision_without_mixing_domains() -> None:
    """A workload cause may carry a distinct PostgreSQL resource target."""
    backend = metrics_tool(lambda _operation, _parameters: {"records": [{"acquisition": 2.0}]})
    db_registry = ReadOnlyToolRegistry(
        (
            RegisteredTool(
                name="db_connection_pressure",
                version="1",
                operation="db_connection_pressure",
                source_type=EvidenceSourceType.METRIC,
                tool=backend,
                argument_model=ServiceArgs,
            ),
        )
    )

    def submit(model_request: ModelRequest) -> dict[str, object]:
        context = json.loads(model_request.messages[1].content)
        return {
            "decision": DecisionType.SUBMIT_HYPOTHESIS,
            "hypothesis": {
                "symptom_component": "payment-service",
                "causal_component": "payment-service",
                "causal_resource": "postgresql",
                "mechanism": "database_connection_pressure",
                "structured_trigger": {
                    "trigger_type": "DB_CONNECTION_PRESSURE",
                    "trigger_component": "payment-service",
                    "trigger_resource": "postgresql",
                },
                "causal_summary": "payment workload is waiting on PostgreSQL connections",
                "evidence_ids": [context["evidence"][0]["evidence_id"]],
            },
        }

    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [
                    {
                        "tool": "db_connection_pressure",
                        "arguments": {"service": "payment-service"},
                    }
                ],
            },
            submit,
        ]
    )
    result = InvestigationRuntime(provider, db_registry, a1_protocol=True).run(incident())

    assert result.causal_hypothesis is not None
    assert result.causal_hypothesis["causal_component"] == "payment-service"
    assert result.causal_hypothesis["causal_resource"] == "postgresql"
    assert result.evidence[0].target_workload == "payment-service"
    assert result.evidence[0].target_resource is None


@pytest.mark.parametrize("model_calls,tool_budget", [(3, 8), (4, 10), (5, 12), (6, 16)])
def test_a1_budget_matrix_remains_bounded_and_terminal_only(
    model_calls: int, tool_budget: int
) -> None:
    """Candidate A1 envelopes expose accurate budgets without overruns."""
    responses: list[dict[str, Any]] = [
        {
            "decision": DecisionType.CALL_TOOLS,
            "requests": [
                {
                    "tool": "service_latency" if index == 0 else f"metric_{index}",
                    "arguments": {"service": "order-service"},
                }
            ],
        }
        for index in range(model_calls - 1)
    ]
    responses.append(
        {
            "decision": DecisionType.STOP,
            "stop": {
                "stop_reason": StopReason.INSUFFICIENT_EVIDENCE,
                "considered_components": [WorkloadComponentId.ORDER_SERVICE],
                "considered_resources": [],
                "missing_evidence_categories": [],
            },
        }
    )
    provider = FakeModelProvider(responses)
    result = InvestigationRuntime(
        provider,
        registry(count=model_calls - 1),
        limits=InvestigationLimits(
            max_model_calls=model_calls,
            max_tool_calls=tool_budget,
            max_agent_turns=model_calls,
        ),
        a1_protocol=True,
    ).run(incident())

    assert result.termination_reason is TerminationReason.AGENT_STOPPED
    assert result.usage.model_calls == model_calls
    assert result.usage.tool_calls == model_calls - 1
    assert all(
        request.allowed_decisions == ("SUBMIT_HYPOTHESIS", "STOP")
        for request in provider.requests[-1:]
    )


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


def test_a1_unknown_workload_target_fails_before_backend_execution() -> None:
    """Canonical target validation rejects unknown workloads before dispatch."""
    backend_calls = 0

    def backend(_operation: str, _parameters: dict[str, Any]) -> dict[str, object]:
        nonlocal backend_calls
        backend_calls += 1
        return {"records": [{"p95": 1.0}]}

    target_tool = RegisteredTool(
        name="service_latency",
        version="1",
        operation="service_latency",
        source_type=EvidenceSourceType.METRIC,
        tool=metrics_tool(backend),
        argument_model=ServiceArgs,
        target_argument="service",
        target_topology=DEFAULT_TOPOLOGY,
    )
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [
                    {"tool": "service_latency", "arguments": {"service": "unknown-service"}}
                ],
            }
        ]
    )

    result = InvestigationRuntime(
        provider,
        ReadOnlyToolRegistry((target_tool,)),
        a1_protocol=True,
    ).run(incident())

    assert result.termination_reason is TerminationReason.INVALID_DECISION
    assert result.error_code == "TOOL_ARGUMENT_SCHEMA_INVALID"
    assert result.usage.tool_calls == 0
    assert backend_calls == 0


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


def test_partial_tool_batch_preserves_attempts_evidence_and_typed_failure() -> None:
    """A failed sibling tool does not erase successful work from the same batch."""

    class PartialTool:
        def __init__(self, name: str, fails: bool = False) -> None:
            self.name = name
            self.version = "1"
            self._fails = fails

        def run(self, _request: Any) -> dict[str, Any]:
            if self._fails:
                raise ValueError("deterministic backend query rejection")
            return {"records": [{"tool": self.name}]}

    tools = tuple(
        RegisteredTool(
            name="service_latency" if index == 0 else f"metric_{index}",
            version="1",
            operation="service_latency",
            source_type=EvidenceSourceType.METRIC,
            tool=PartialTool(
                "service_latency" if index == 0 else f"metric_{index}",
                fails=index in {4, 5},
            ),
            argument_model=ServiceArgs,
        )
        for index in range(6)
    )
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [{"tool": "service_latency", "arguments": {"service": "order-worker"}}],
            },
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [
                    {
                        "tool": f"metric_{index}",
                        "arguments": {"service": "order-worker"},
                    }
                    for index in range(1, 6)
                ],
            },
        ]
    )
    audit = InMemoryInvestigationAuditSink()

    result = InvestigationRuntime(
        provider,
        ReadOnlyToolRegistry(tools),
        audit_sink=audit,
    ).run(incident())

    assert result.termination_reason is TerminationReason.TOOL_FAILURE
    assert result.error_code == "INVALID_QUERY"
    assert result.validation_stage is not None
    assert result.validation_stage.value == "TOOL_EXECUTION"
    assert result.usage.tool_calls == 6
    assert len(result.evidence) == 4
    assert len(audit.turn_records) == 2
    assert audit.turn_records[1].tool_calls_attempted == 5
    assert audit.turn_records[1].tool_calls_succeeded == 3
    assert audit.turn_records[1].tool_calls_failed == 2
    assert len(audit.turn_records[1].evidence_ids_created) == 3


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


def test_a1_decision_envelope_accepts_wider_explicit_batches() -> None:
    """A1 has a wider versioned envelope while the legacy envelope stays at eight."""
    decision = A1InvestigationDecision.model_validate(
        {
            "decision": DecisionType.CALL_TOOLS,
            "requests": [{"tool": f"tool-{index}"} for index in range(12)],
        }
    )
    assert len(decision.requests) == 12

    with pytest.raises(ValueError):
        A1InvestigationDecision.model_validate(
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [{"tool": f"tool-{index}"} for index in range(21)],
            }
        )


def test_investigation_limits_keep_v0_defaults_and_allow_a1_candidates() -> None:
    """Limits are configurable within hard ceilings instead of encoding 3/8."""
    assert InvestigationLimits().max_model_calls == 3
    assert InvestigationLimits().max_tool_calls == 8
    candidate = InvestigationLimits(max_model_calls=5, max_tool_calls=12, max_agent_turns=5)
    assert candidate.max_model_calls == 5
    assert candidate.max_tool_calls == 12
    with pytest.raises(ValueError):
        InvestigationLimits(max_model_calls=9)
    with pytest.raises(ValueError):
        InvestigationLimits(max_tool_calls=21)


def test_runtime_uses_wider_decision_envelope_for_explicit_a1_tool_budget() -> None:
    """An explicit wider A1 budget reaches the provider response schema."""
    provider = FakeModelProvider(
        [
            {"decision": DecisionType.CALL_TOOLS, "requests": _tool_requests(12)},
            {
                "decision": DecisionType.STOP,
                "stop_reason": StopReason.INSUFFICIENT_EVIDENCE,
            },
        ]
    )

    result = InvestigationRuntime(
        provider,
        registry(count=12),
        limits=InvestigationLimits(max_model_calls=2, max_tool_calls=12),
    ).run(incident())

    assert result.usage.tool_calls == 12
    assert provider.requests[0].response_schema["properties"]["requests"]["maxItems"] == 20


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


def test_novel_batch_above_remaining_budget_fails_closed() -> None:
    """Only novel executions are compared with the remaining tool budget."""
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [{"tool": "metric_8", "arguments": {"service": "order-worker"}}],
            },
            {"decision": DecisionType.CALL_TOOLS, "requests": _tool_requests(8)},
        ]
    )

    result = InvestigationRuntime(provider, registry(9)).run(incident())

    assert result.termination_reason is TerminationReason.TOOL_CALL_LIMIT
    assert result.error_code == "TOOL_BUDGET_EXCEEDED"
    assert result.usage.tool_calls == 1


def test_all_duplicate_batch_does_not_consume_remaining_budget() -> None:
    """A duplicate request is suppressed rather than treated as new work."""
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [{"tool": "service_latency", "arguments": {"service": "order-worker"}}],
            },
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [{"tool": "service_latency", "arguments": {"service": "order-worker"}}],
            },
            {"decision": DecisionType.STOP, "stop_reason": "insufficient_evidence"},
        ]
    )

    result = InvestigationRuntime(provider, registry(8)).run(incident())

    assert result.termination_reason is TerminationReason.AGENT_STOPPED
    assert result.error_code is None
    assert result.usage.tool_calls == 1
    assert result.usage.duplicate_requests_suppressed == 1


def test_exact_duplicate_tool_requests_are_suppressed_without_reexecution() -> None:
    """Exact duplicates reuse the original observation and do not reexecute."""
    audit = InMemoryInvestigationAuditSink()
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [
                    {"tool": "service_latency", "arguments": {"service": "order-worker"}},
                    {"tool": "service_latency", "arguments": {"service": "order-worker"}},
                ],
            },
            {"decision": DecisionType.STOP, "stop_reason": "insufficient_evidence"},
        ]
    )

    result = InvestigationRuntime(provider, registry(), audit_sink=audit).run(incident())

    assert result.termination_reason is TerminationReason.AGENT_STOPPED
    assert result.error_code is None
    assert result.usage.tool_calls == 1
    assert result.usage.tool_requests_total == 2
    assert result.usage.duplicate_requests_suppressed == 1
    assert len(result.evidence) == 1
    assert result.turns[0]["summaries"][1]["status"] == "SKIPPED_DUPLICATE"
    assert result.turns[0]["summaries"][1]["reused_evidence_ids"] == [
        str(result.evidence[0].evidence_id)
    ]
    assert result.turns[0]["requested_tool_names"] == ["service_latency", "service_latency"]
    assert audit.turn_records[0].requested_tool_names == [
        "service_latency",
        "service_latency",
    ]
    assert audit.turn_records[0].request_audits[1]["status"] == "SKIPPED_DUPLICATE"
    assert audit.turn_records[0].tool_calls_attempted == 1
    assert audit.turn_records[0].tool_calls_succeeded == 1
    assert audit.turn_records[0].tool_calls_failed == 0


def test_mixed_batch_suppresses_duplicate_and_executes_novel_requests() -> None:
    """A duplicate must not discard valid sibling investigation work."""
    args = {"service": "order-worker"}
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [
                    {"tool": name, "arguments": args}
                    for name in ("service_latency", "metric_1", "metric_2")
                ],
            },
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [
                    {"tool": name, "arguments": args}
                    for name in ("service_latency", "metric_3", "metric_4", "metric_5")
                ],
            },
            {"decision": DecisionType.STOP, "stop_reason": "insufficient_evidence"},
        ]
    )

    result = InvestigationRuntime(provider, registry(6)).run(incident())

    assert result.termination_reason is TerminationReason.AGENT_STOPPED
    assert result.usage.tool_requests_total == 7
    assert result.usage.tool_calls == 6
    assert result.usage.duplicate_requests_suppressed == 1
    assert len(result.evidence) == 6
    assert result.turns[1]["summaries"][0]["status"] == "SKIPPED_DUPLICATE"
    assert result.turns[1]["tool_calls_attempted"] == 3


def test_current_state_requests_can_refresh_across_turns() -> None:
    """Current-state tools are reusable only within a single batch."""
    args = {"service": "order-worker"}
    provider = FakeModelProvider(
        [
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [{"tool": "service_latency", "arguments": args}],
            },
            {
                "decision": DecisionType.CALL_TOOLS,
                "requests": [{"tool": "service_latency", "arguments": args}],
            },
            {"decision": DecisionType.STOP, "stop_reason": "insufficient_evidence"},
        ]
    )

    result = InvestigationRuntime(
        provider,
        registry(repeat_policy=ToolRepeatPolicy.CURRENT_STATE),
    ).run(incident())

    assert result.termination_reason is TerminationReason.AGENT_STOPPED
    assert result.usage.tool_calls == 2
    assert result.usage.duplicate_requests_suppressed == 0


def test_canonical_nullable_arguments_share_identity() -> None:
    """Omitted and provider-null optional values canonicalize identically."""
    from packages.investigation.duplicates import make_tool_request_identity
    from packages.investigation.tool_contracts import ServiceWindowArgs

    first = ServiceWindowArgs.model_validate({"service": "order-worker"}).model_dump(
        mode="json", exclude_none=True
    )
    second = ServiceWindowArgs.model_validate(
        {"service": "order-worker", "range_seconds": None}
    ).model_dump(mode="json", exclude_none=True)
    window = {"starts_at": "2026-09-12T00:00:00Z", "ends_at": "2026-09-12T00:05:00Z"}

    assert (
        make_tool_request_identity("service_logs", first, window).identity_hash
        == make_tool_request_identity("service_logs", second, window).identity_hash
    )


def test_different_observation_windows_are_not_duplicate_identities() -> None:
    """The authoritative observation scope participates in fixed-window identity."""
    from packages.investigation.duplicates import make_tool_request_identity

    first = make_tool_request_identity(
        "kafka_consumer_lag",
        {"consumer": "order-worker"},
        {"starts_at": "2026-09-12T00:00:00Z", "ends_at": "2026-09-12T00:05:00Z"},
    )
    second = make_tool_request_identity(
        "kafka_consumer_lag",
        {"consumer": "order-worker"},
        {"starts_at": "2026-09-12T00:01:00Z", "ends_at": "2026-09-12T00:06:00Z"},
    )

    assert first.identity_hash != second.identity_hash
