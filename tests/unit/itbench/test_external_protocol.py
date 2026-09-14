"""Offline regression tests for the ITBench-native E2 boundary."""

import json
from pathlib import Path
from typing import cast
from uuid import uuid4

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
    ModelMessage,
    OpenAIProvider,
)


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
