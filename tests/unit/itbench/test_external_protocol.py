"""Offline regression tests for the ITBench-native E2 boundary."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.evals.itbench import (
    ExternalInvestigationRuntime,
    ITBenchDecisionType,
    ITBenchEvidenceCategory,
    ITBenchExternalToolRegistry,
    ITBenchInvestigationDecisionV1,
    ITBenchLiteDataset,
    ITBenchScenario,
    ITBenchSnapshotBackend,
    build_observable_incident,
)
from packages.evals.itbench.external_context import build_external_context
from packages.investigation.contracts import InvestigationLimits
from packages.provider import (
    FakeModelProvider,
    LiveModelBudget,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    OpenAIProvider,
    ProviderError,
    ProviderErrorCode,
)
from packages.provider import contracts as provider_contracts
from packages.provider import openai as openai_module
from packages.provider.openai import LiveModelConfig


def _scenario(tmp_path: Path) -> ITBenchScenario:
    scenario_dir = tmp_path / "Scenario-1"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "alerts.json").write_text(
        json.dumps(
            {
                "data": {
                    "alerts": [
                        {
                            "state": "firing",
                            "activeAt": "2025-12-15T17:25:19Z",
                            "labels": {
                                "alertname": "RequestErrorRate",
                                "namespace": "otel-demo",
                                "service_name": "frontend",
                                "severity": "critical",
                            },
                            "annotations": {"description": "errors"},
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    header = "Timestamp\tServiceName\tBody\n"
    body = '{"metadata":{"name":"frontend","namespace":"otel-demo"},"kind":"Service"}'
    for filename in (
        "k8s_events_raw.tsv",
        "k8s_objects_raw.tsv",
        "otel_logs_raw.tsv",
        "otel_traces_raw.tsv",
    ):
        (scenario_dir / filename).write_text(header + f"2025\tfrontend\t{body}\n", encoding="utf-8")
    (scenario_dir / "metric.tsv").write_text(
        header + "2025\tfrontend\tdegraded\n", encoding="utf-8"
    )
    return ITBenchScenario(
        scenario_id="Scenario-1",
        snapshot_path=str(scenario_dir),
        evidence_categories=tuple(ITBenchEvidenceCategory),
        evidence_files={
            ITBenchEvidenceCategory.ALERTS: ("alerts.json",),
            ITBenchEvidenceCategory.METRICS: ("metric.tsv",),
            ITBenchEvidenceCategory.K8S_EVENTS: ("k8s_events_raw.tsv",),
            ITBenchEvidenceCategory.K8S_OBJECTS: ("k8s_objects_raw.tsv",),
            ITBenchEvidenceCategory.LOGS: ("otel_logs_raw.tsv",),
            ITBenchEvidenceCategory.TRACES: ("otel_traces_raw.tsv",),
        },
    )


def test_external_contract_accepts_multiple_native_entities_and_stop() -> None:
    evidence_id = uuid4()
    diagnosis = ITBenchInvestigationDecisionV1.model_validate(
        {
            "decision": ITBenchDecisionType.SUBMIT_DIAGNOSIS,
            "root_causes": [
                {
                    "entity": "otel-demo/ConfigMap/flags",
                    "causal_summary": "observable configuration evidence",
                    "evidence_ids": [evidence_id],
                },
                {
                    "entity": "_cluster/Node/node-a",
                    "causal_summary": "observable node evidence",
                    "evidence_ids": [evidence_id],
                },
            ],
        }
    )
    assert len(diagnosis.root_causes) == 2
    stopped = ITBenchInvestigationDecisionV1.model_validate(
        {
            "decision": ITBenchDecisionType.STOP,
            "stop": {
                "stop_reason": "insufficient_evidence",
                "evidence_categories_considered": ["alerts"],
                "entities_considered": [],
                "missing_evidence_categories": ["metrics"],
            },
        }
    )
    assert stopped.stop is not None


def test_external_context_does_not_inject_internal_topology(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    incident, alerts = build_observable_incident(backend)
    registry = ITBenchExternalToolRegistry(backend)
    context = build_external_context(backend, incident, alerts, registry.descriptors())
    assert "DEFAULT_TOPOLOGY" not in context
    assert "order-worker" not in context
    assert "ground_truth" not in context.casefold()


def test_external_registry_is_read_only_and_bounded(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=800
    )
    registry = ITBenchExternalToolRegistry(backend)
    assert "ground_truth" not in json.dumps(registry.descriptors()).casefold()
    assert all(name not in registry.names() for name in ("shell", "filesystem", "sql", "kubectl"))
    result = registry.invoke("itbench_entity_search", {"limit": 50})
    assert len(json.dumps(result).encode()) <= 800


def test_external_runtime_fake_provider_never_needs_ground_truth(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    provider = FakeModelProvider(
        [
            {
                "decision": "CALL_TOOLS",
                "requests": [{"tool": "itbench_alert_summary", "arguments": {"limit": 1}}],
            },
            {
                "decision": "STOP",
                "stop": {
                    "stop_reason": "insufficient_evidence",
                    "evidence_categories_considered": ["alerts"],
                    "entities_considered": [],
                    "missing_evidence_categories": ["metrics"],
                },
            },
        ]
    )
    result = ExternalInvestigationRuntime(provider, registry.investigation_registry(), backend).run(
        incident, alerts
    )
    assert result.terminal == "STOP"
    assert result.evidence
    assert all("ground_truth" not in json.dumps(item).casefold() for item in result.evidence)


def test_external_runtime_rejects_any_fabricated_root_cause_evidence_id(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    provider = FakeModelProvider(
        [
            {
                "decision": "CALL_TOOLS",
                "requests": [{"tool": "itbench_alert_summary", "arguments": {"limit": 1}}],
            },
            lambda request: {
                "decision": "SUBMIT_DIAGNOSIS",
                "root_causes": [
                    {
                        "entity": "otel-demo/Service/frontend",
                        "causal_summary": "test",
                        "evidence_ids": [
                            json.loads(request.messages[-1].content)["evidence"][0]["evidence_id"],
                            str(uuid4()),
                        ],
                    }
                ],
            },
        ]
    )
    result = ExternalInvestigationRuntime(provider, registry.investigation_registry(), backend).run(
        incident, alerts
    )
    assert result.terminal == "INVALID_DECISION"


def test_external_provider_schema_builds_without_provider_construction(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    runtime = ExternalInvestigationRuntime(
        FakeModelProvider([]),
        registry.investigation_registry(),
        backend,
        limits=InvestigationLimits(
            max_model_calls=5, max_tool_calls=12, max_agent_turns=5, max_wall_time_seconds=180
        ),
    )
    request = runtime.build_request(incident, alerts, run_id=incident.incident_id)
    payload = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
    assert {item["name"] for item in payload["tools"]} == {
        "request_itbench_tools",
        "submit_itbench_diagnosis",
        "stop_itbench_investigation",
    }
    assert request.messages[0] == ModelMessage(role="system", content=request.messages[0].content)


def test_external_provider_normalizes_strict_function_output(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    runtime = ExternalInvestigationRuntime(
        FakeModelProvider([]), registry.investigation_registry(), backend
    )
    request = runtime.build_request(incident, alerts, run_id=incident.incident_id)
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "name": "stop_itbench_investigation",
                "arguments": json.dumps(
                    {
                        "reason": "insufficient evidence",
                        "stop": {
                            "stop_reason": "insufficient_evidence",
                            "evidence_categories_considered": ["alerts"],
                            "entities_considered": [],
                            "missing_evidence_categories": ["metrics"],
                        },
                    }
                ),
            }
        ],
    }
    response = OpenAIProvider.__new__(OpenAIProvider)._normalize_response(request, raw, 0.0)
    assert response.structured_output["decision"] == "STOP"
    assert response.structured_output["stop"]["stop_reason"] == "insufficient_evidence"


def _external_request(tmp_path: Path) -> tuple[ModelRequest, ITBenchSnapshotBackend]:
    """Build one real external protocol request for provider-envelope tests."""
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    runtime = ExternalInvestigationRuntime(
        FakeModelProvider([]), registry.investigation_registry(), backend
    )
    return runtime.build_request(incident, alerts, run_id=incident.incident_id), backend


def _external_function_call(name: str, arguments: dict[str, object]) -> SimpleNamespace:
    """Build one synthetic Responses function call without a provider."""
    return SimpleNamespace(
        type="function_call", name=name, arguments=json.dumps(arguments, sort_keys=True)
    )


def _external_envelope(*calls: SimpleNamespace) -> SimpleNamespace:
    """Build a completed Responses envelope with bounded synthetic usage."""
    return SimpleNamespace(
        id="itbench-response",
        status="completed",
        error=None,
        incomplete_details=None,
        output=list(calls),
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


def _normalize_external(tmp_path: Path, name: str, arguments: dict[str, object]) -> ModelResponse:
    """Exercise the actual OpenAIProvider normalization path for ITBench."""
    request, _backend = _external_request(tmp_path)
    raw = _external_envelope(_external_function_call(name, arguments))
    return OpenAIProvider.__new__(OpenAIProvider)._normalize_response(request, raw, 0.0)


def _call_tools_arguments(*, pattern: object = None, limit: object = 1) -> dict[str, object]:
    """Return wire-valid arguments, including nullable optional fields."""
    return {
        "reason": "bounded alert evidence is needed",
        "tool_requests": [
            {
                "tool": "itbench_alert_summary",
                "arguments": {"pattern": pattern, "limit": limit},
            }
        ],
    }


def test_external_call_tools_normalization_has_no_legacy_fields(tmp_path: Path) -> None:
    """The historical E2 payload is rejected and the repaired payload is valid."""
    with pytest.raises(ValidationError):
        ITBenchInvestigationDecisionV1.model_validate(
            {"decision": "CALL_TOOLS", "requests": [], "hypothesis": None}
        )

    response = _normalize_external(
        tmp_path,
        "request_itbench_tools",
        _call_tools_arguments(pattern=None),
    )
    assert response.structured_output == {
        "decision": "CALL_TOOLS",
        "requests": [
            {"tool": "itbench_alert_summary", "arguments": {"limit": 1}},
        ],
        "root_causes": [],
        "stop": None,
    }
    decision = ITBenchInvestigationDecisionV1.model_validate_json(
        json.dumps(response.structured_output)
    )
    assert decision.decision.value == "CALL_TOOLS"
    assert "hypothesis" not in response.structured_output


def test_external_submit_diagnosis_normalization_is_canonical(tmp_path: Path) -> None:
    """Diagnosis normalization uses only the external envelope vocabulary."""
    evidence_id = uuid4()
    response = _normalize_external(
        tmp_path,
        "submit_itbench_diagnosis",
        {
            "reason": "the evidence supports one cause",
            "root_causes": [
                {
                    "entity": "otel-demo/ConfigMap/checkout-config",
                    "causal_summary": "observable configuration failure",
                    "evidence_ids": [str(evidence_id)],
                }
            ],
        },
    )
    assert set(response.structured_output) == {"decision", "requests", "root_causes", "stop"}
    decision = ITBenchInvestigationDecisionV1.model_validate_json(
        json.dumps(response.structured_output)
    )
    assert decision.decision.value == "SUBMIT_DIAGNOSIS"
    assert decision.requests == []
    assert len(decision.root_causes) == 1
    assert decision.stop is None


def test_external_stop_normalization_is_canonical(tmp_path: Path) -> None:
    """STOP normalization contains no legacy stop or hypothesis fields."""
    response = _normalize_external(
        tmp_path,
        "stop_itbench_investigation",
        {
            "reason": "evidence is insufficient",
            "stop": {
                "stop_reason": "insufficient_evidence",
                "evidence_categories_considered": ["alerts"],
                "entities_considered": [],
                "missing_evidence_categories": ["metrics"],
            },
        },
    )
    assert set(response.structured_output) == {"decision", "requests", "root_causes", "stop"}
    decision = ITBenchInvestigationDecisionV1.model_validate_json(
        json.dumps(response.structured_output)
    )
    assert decision.decision.value == "STOP"
    assert decision.requests == []
    assert decision.root_causes == []
    assert decision.stop is not None


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        (
            "request_itbench_tools",
            _call_tools_arguments(),
            {"decision", "requests", "root_causes", "stop"},
        ),
        (
            "submit_itbench_diagnosis",
            {
                "reason": "supported",
                "root_causes": [
                    {
                        "entity": "otel-demo/Pod/frontend",
                        "causal_summary": "pod evidence",
                        "evidence_ids": [str(uuid4())],
                    }
                ],
            },
            {"decision", "requests", "root_causes", "stop"},
        ),
        (
            "stop_itbench_investigation",
            {
                "reason": "insufficient",
                "stop": {
                    "stop_reason": "insufficient_evidence",
                    "evidence_categories_considered": [],
                    "entities_considered": [],
                    "missing_evidence_categories": [],
                },
            },
            {"decision", "requests", "root_causes", "stop"},
        ),
    ],
)
def test_external_normalization_matrix_exact_field_vocabulary(
    tmp_path: Path,
    name: str,
    arguments: dict[str, object],
    expected: set[str],
) -> None:
    """All three external decisions satisfy the same normalized envelope shape."""
    response = _normalize_external(tmp_path, name, arguments)
    assert set(response.structured_output) == expected
    ITBenchInvestigationDecisionV1.model_validate_json(json.dumps(response.structured_output))


def test_normalized_provider_contract_guard_rejects_adapter_extra_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-normalization guard catches adapter fields absent from the request schema."""
    request, _backend = _external_request(tmp_path)
    metadata = provider_contracts.ResponseEnvelopeMetadata(response_id="guard-test")

    def invalid_normalization(
        _request: ModelRequest, _raw: object
    ) -> tuple[dict[str, Any], provider_contracts.ResponseEnvelopeMetadata]:
        return (
            {"decision": "CALL_TOOLS", "requests": [], "hypothesis": None},
            metadata,
        )

    monkeypatch.setattr(openai_module, "_extract_decision_function", invalid_normalization)
    with pytest.raises(ProviderError) as error:
        OpenAIProvider.__new__(OpenAIProvider)._normalize_response(
            request, _external_envelope(), 0.0
        )
    assert error.value.code is ProviderErrorCode.SCHEMA_VALIDATION_FAILED
    assert error.value.metadata is not None
    assert error.value.metadata.schema_error_path == "$.hypothesis"


def test_external_decision_transport_matrix_is_twelve_of_twelve(tmp_path: Path) -> None:
    """Each external decision passes four offline transport/normalization gates."""
    cases: list[tuple[str, dict[str, object], set[str]]] = [
        (
            "request_itbench_tools",
            _call_tools_arguments(),
            {"decision", "requests", "root_causes", "stop"},
        ),
        (
            "submit_itbench_diagnosis",
            {
                "reason": "supported",
                "root_causes": [
                    {
                        "entity": "otel-demo/Pod/frontend",
                        "causal_summary": "pod evidence",
                        "evidence_ids": [str(uuid4())],
                    }
                ],
            },
            {"decision", "requests", "root_causes", "stop"},
        ),
        (
            "stop_itbench_investigation",
            {
                "reason": "insufficient",
                "stop": {
                    "stop_reason": "insufficient_evidence",
                    "evidence_categories_considered": [],
                    "entities_considered": [],
                    "missing_evidence_categories": [],
                },
            },
            {"decision", "requests", "root_causes", "stop"},
        ),
    ]
    checks = 0
    for index, (name, arguments, fields) in enumerate(cases):
        request, _backend = _external_request(tmp_path / str(index))
        wire = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
        assert name in {item["name"] for item in wire["tools"]}
        checks += 1
        response = OpenAIProvider.__new__(OpenAIProvider)._normalize_response(
            request, _external_envelope(_external_function_call(name, arguments)), 0.0
        )
        checks += 1
        assert set(response.structured_output) == fields
        checks += 1
        ITBenchInvestigationDecisionV1.model_validate_json(json.dumps(response.structured_output))
        checks += 1
    assert checks == 12


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "reason": "bad tool",
            "tool_requests": [{"tool": "not_registered", "arguments": {"limit": 1}}],
        },
        {
            "reason": "wrong type",
            "tool_requests": [{"tool": "itbench_alert_summary", "arguments": {"limit": "1"}}],
        },
        {
            "reason": "extra argument",
            "tool_requests": [
                {
                    "tool": "itbench_alert_summary",
                    "arguments": {"pattern": None, "limit": 1, "extra": 1},
                }
            ],
        },
        {
            "reason": "missing argument",
            "tool_requests": [{"tool": "itbench_alert_summary", "arguments": {"pattern": None}}],
        },
    ],
)
def test_external_call_tools_wire_validation_fails_closed(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    """Malformed external tool requests never reach normalized runtime output."""
    with pytest.raises(ProviderError) as error:
        _normalize_external(tmp_path, "request_itbench_tools", arguments)
    assert error.value.code is ProviderErrorCode.FUNCTION_ARGUMENTS_SCHEMA_INVALID


def test_external_multiple_and_unexpected_functions_fail_closed(tmp_path: Path) -> None:
    """The real envelope parser rejects ambiguous and unregistered functions."""
    request, _backend = _external_request(tmp_path / "multiple")
    first = _external_function_call("request_itbench_tools", _call_tools_arguments(pattern=None))
    second = _external_function_call(
        "stop_itbench_investigation",
        {
            "reason": "stop",
            "stop": {
                "stop_reason": "insufficient_evidence",
                "evidence_categories_considered": [],
                "entities_considered": [],
                "missing_evidence_categories": [],
            },
        },
    )
    with pytest.raises(ProviderError) as multiple:
        OpenAIProvider.__new__(OpenAIProvider)._normalize_response(
            request, _external_envelope(first, second), 0.0
        )
    assert multiple.value.code is ProviderErrorCode.MULTIPLE_DECISION_FUNCTION_CALLS

    request, _backend = _external_request(tmp_path / "unexpected")
    with pytest.raises(ProviderError) as unexpected:
        OpenAIProvider.__new__(OpenAIProvider)._normalize_response(
            request,
            _external_envelope(_external_function_call("not_a_decision", {})),
            0.0,
        )
    assert unexpected.value.code is ProviderErrorCode.UNEXPECTED_FUNCTION_CALL


def test_external_final_turn_rejects_call_tools(tmp_path: Path) -> None:
    """External terminal-turn gating applies through the Responses parser."""
    request, _backend = _external_request(tmp_path)
    terminal_request = request.model_copy(
        update={"allowed_decisions": ("SUBMIT_DIAGNOSIS", "STOP")}
    )
    with pytest.raises(ProviderError) as error:
        OpenAIProvider.__new__(OpenAIProvider)._normalize_response(
            terminal_request,
            _external_envelope(
                _external_function_call("request_itbench_tools", _call_tools_arguments())
            ),
            0.0,
        )
    assert error.value.code is ProviderErrorCode.UNEXPECTED_FUNCTION_CALL


def test_legacy_request_normalization_remains_protocol_local(tmp_path: Path) -> None:
    """Repairing ITBench does not add external fields to the legacy envelope."""
    from packages.investigation import InvestigationDecision

    request, _backend = _external_request(tmp_path)
    legacy_request = request.model_copy(
        update={
            "response_schema_name": "investigation_decision",
            "response_schema": InvestigationDecision.model_json_schema(),
            "allowed_decisions": ("CALL_TOOLS", "SUBMIT_HYPOTHESIS", "STOP"),
            "allowed_tool_names": ("itbench_alert_summary",),
        }
    )
    response = OpenAIProvider.__new__(OpenAIProvider)._normalize_response(
        legacy_request,
        _external_envelope(
            _external_function_call("request_investigation_tools", _call_tools_arguments())
        ),
        0.0,
    )
    assert response.structured_output["decision"] == "CALL_TOOLS"
    assert "root_causes" not in response.structured_output
    assert "hypothesis" in response.structured_output


class _QueueResponsesTransport:
    """Offline Responses transport that exercises OpenAIProvider.complete."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        return response(self.calls[-1]) if callable(response) else response


def _live_external_provider(tmp_path: Path, transport: _QueueResponsesTransport) -> OpenAIProvider:
    """Build an enabled provider with an isolated temporary ledger."""
    return OpenAIProvider(
        budget=LiveModelBudget(5, ledger_path=str(tmp_path / "provider-ledger.json")),
        config=LiveModelConfig(enabled=True, model="gpt-5.6-luna", reasoning_effort="none"),
        transport=transport,
        max_retry=0,
    )


def test_real_openai_provider_external_call_tools_to_stop(tmp_path: Path) -> None:
    """The real provider adapter and runtime complete CALL_TOOLS -> STOP offline."""
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    transport = _QueueResponsesTransport(
        [
            _external_envelope(
                _external_function_call("request_itbench_tools", _call_tools_arguments())
            ),
            _external_envelope(
                _external_function_call(
                    "stop_itbench_investigation",
                    {
                        "reason": "done",
                        "stop": {
                            "stop_reason": "insufficient_evidence",
                            "evidence_categories_considered": ["alerts"],
                            "entities_considered": [],
                            "missing_evidence_categories": ["metrics"],
                        },
                    },
                )
            ),
        ]
    )
    result = ExternalInvestigationRuntime(
        _live_external_provider(tmp_path, transport),
        registry.investigation_registry(),
        backend,
        limits=InvestigationLimits(
            max_model_calls=2, max_tool_calls=12, max_agent_turns=2, max_wall_time_seconds=180
        ),
    ).run(incident, alerts)
    assert result.terminal == "STOP"
    assert len(result.evidence) == 1
    assert len(transport.calls) == 2
    assert (tmp_path / "provider-ledger.json").read_text(encoding="utf-8") == '{"calls_used": 2}'


def test_real_openai_provider_external_call_tools_to_diagnosis(tmp_path: Path) -> None:
    """The real provider adapter preserves runtime evidence into diagnosis."""
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)

    def diagnosis_response(request: dict[str, object]) -> SimpleNamespace:
        messages = request["input"]
        assert isinstance(messages, list)
        user_message = messages[-1]
        assert isinstance(user_message, dict)
        context = json.loads(str(user_message["content"]))
        evidence_id = context["evidence"][0]["evidence_id"]
        return _external_envelope(
            _external_function_call(
                "submit_itbench_diagnosis",
                {
                    "reason": "the tool evidence supports the entity",
                    "root_causes": [
                        {
                            "entity": "otel-demo/Service/frontend",
                            "causal_summary": "runtime-owned alert evidence",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                },
            )
        )

    transport = _QueueResponsesTransport(
        [
            _external_envelope(
                _external_function_call("request_itbench_tools", _call_tools_arguments())
            ),
            diagnosis_response,
        ]
    )
    result = ExternalInvestigationRuntime(
        _live_external_provider(tmp_path, transport),
        registry.investigation_registry(),
        backend,
        limits=InvestigationLimits(
            max_model_calls=2, max_tool_calls=12, max_agent_turns=2, max_wall_time_seconds=180
        ),
    ).run(incident, alerts)
    assert result.terminal == "SUBMIT_DIAGNOSIS"
    assert result.decision is not None
    assert len(result.decision.root_causes) == 1
    assert len(result.evidence) == 1
    assert len(transport.calls) == 2
