"""Model-free E5 regressions for generic external diagnosis improvements."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from packages.evals.itbench import (
    ExternalInvestigationRuntime,
    ITBenchEvidenceCategory,
    ITBenchExternalToolRegistry,
    ITBenchLiteDataset,
    ITBenchScenario,
    ITBenchSnapshotBackend,
    adapt_external_output,
    build_observable_incident,
)
from packages.evals.itbench.external_contracts import (
    ITBenchDecisionType,
    ITBenchInvestigationDecisionV2,
)
from packages.investigation.contracts import InvestigationLimits
from packages.provider import FakeModelProvider, OpenAIProvider


def _scenario(tmp_path: Path) -> ITBenchScenario:
    root = tmp_path / "Scenario-999"
    root.mkdir()
    (root / "alerts.json").write_text(
        json.dumps(
            {
                "alerts": [
                    {
                        "state": "firing",
                        "activeAt": "2025-01-01T00:00:00Z",
                        "labels": {"alertname": "ErrorRate", "service": "frontend"},
                        "annotations": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    header = "Timestamp\tServiceName\tBody\tValue\tTraceId\n"
    object_body = json.dumps(
        {"kind": "ConfigMap", "metadata": {"namespace": "demo", "name": "checkout-config"}}
    )
    (root / "objects.tsv").write_text(
        header + f"2025\tfrontend\t{object_body}\t1\ttrace-1\n", encoding="utf-8"
    )
    (root / "events.tsv").write_text(
        header + f"2025\tfrontend\t{object_body}\t1\ttrace-1\n", encoding="utf-8"
    )
    (root / "metrics.tsv").write_text(
        header + "2025\tfrontend\tmetric\t1\ttrace-1\n2026\tfrontend\tmetric\t9\ttrace-2\n",
        encoding="utf-8",
    )
    (root / "logs.tsv").write_text(
        header + "2025\tfrontend\tconnection timeout\t1\ttrace-1\n", encoding="utf-8"
    )
    (root / "traces.tsv").write_text(
        header + "2025\tfrontend\tspan\t1\ttrace-1\n", encoding="utf-8"
    )
    files: dict[ITBenchEvidenceCategory, tuple[str, ...]] = {
        ITBenchEvidenceCategory.ALERTS: ("alerts.json",),
        ITBenchEvidenceCategory.METRICS: ("metrics.tsv",),
        ITBenchEvidenceCategory.K8S_EVENTS: ("events.tsv",),
        ITBenchEvidenceCategory.K8S_OBJECTS: ("objects.tsv",),
        ITBenchEvidenceCategory.LOGS: ("logs.tsv",),
        ITBenchEvidenceCategory.TRACES: ("traces.tsv",),
    }
    return ITBenchScenario(
        scenario_id="Scenario-999",
        snapshot_path=str(root),
        evidence_categories=tuple(files),
        evidence_files=files,
    )


def _backend(tmp_path: Path) -> ITBenchSnapshotBackend:
    return ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), _scenario(tmp_path), max_rows=5, max_bytes=20_000
    )


def test_structured_entity_search_and_context(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend)
    found = registry.invoke(
        "itbench_entity_search", {"entity": "demo/ConfigMap/checkout-config", "limit": 5}
    )
    assert found["returned_count"] == 1
    context = registry.invoke(
        "itbench_entity_context", {"entity": "demo/ConfigMap/checkout-config", "limit": 5}
    )
    assert context["object_matching_count"] == 1
    assert context["object_records"][0]["record"]["Body"]


def test_metric_aggregate_covers_all_matching_rows(tmp_path: Path) -> None:
    registry = ITBenchExternalToolRegistry(_backend(tmp_path))
    result = registry.invoke("itbench_metric_analysis", {"service": "frontend", "limit": 1})
    assert result["aggregate"] == {
        "count": 2,
        "min": 1.0,
        "max": 9.0,
        "mean": 5.0,
        "first": 1.0,
        "last": 9.0,
        "delta": 8.0,
    }
    assert result["sample_count"] == 1
    assert result["sample_truncated"] is True


def test_v2_evidence_handles_are_runtime_owned(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    provider = FakeModelProvider(
        [
            {
                "decision": "CALL_TOOLS",
                "requests": [{"tool": "itbench_logs", "arguments": {"limit": 1}}],
            },
            lambda request: {
                "decision": "SUBMIT_DIAGNOSIS",
                "root_causes": [
                    {
                        "entity": "demo/ConfigMap/checkout-config",
                        "causal_summary": "config evidence",
                        "evidence_refs": [
                            json.loads(request.messages[-1].content)["evidence"][0]["evidence_ref"]
                        ],
                    }
                ],
            },
        ]
    )
    result = ExternalInvestigationRuntime(
        provider,
        registry.investigation_registry(),
        backend,
        protocol_version="itbench_investigation_decision_v2",
        limits=InvestigationLimits(
            max_model_calls=2, max_tool_calls=12, max_agent_turns=2, max_wall_time_seconds=180
        ),
    ).run(incident, alerts)
    assert result.terminal == "SUBMIT_DIAGNOSIS"
    assert result.protocol == "itbench_investigation_decision_v2"
    assert result.decision is not None
    assert result.decision.root_causes[0].evidence_refs == ["E001"]  # type: ignore[union-attr]
    assert "evidence_id" not in json.dumps(result.turns)
    assert len(adapt_external_output(result).contributing_factor) == 1


@pytest.mark.parametrize("value", ["frontend", "Deployment/frontend", "demo//frontend", "/foo/bar"])
def test_v2_rejects_noncanonical_entities(value: str) -> None:
    with pytest.raises(ValidationError):
        ITBenchInvestigationDecisionV2.model_validate(
            {
                "decision": ITBenchDecisionType.SUBMIT_DIAGNOSIS,
                "root_causes": [
                    {"entity": value, "causal_summary": "x", "evidence_refs": ["E001"]}
                ],
            }
        )


def test_cluster_scoped_entity_round_trip() -> None:
    result = ITBenchInvestigationDecisionV2.model_validate(
        {
            "decision": ITBenchDecisionType.SUBMIT_DIAGNOSIS,
            "root_causes": [
                {
                    "entity": "_cluster/Node/node-a",
                    "causal_summary": "node",
                    "evidence_refs": ["E001"],
                }
            ],
        }
    )
    assert result.root_causes[0].entity == "_cluster/Node/node-a"


def test_v2_openai_envelope_has_exact_contract(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    runtime = ExternalInvestigationRuntime(
        FakeModelProvider([]),
        registry.investigation_registry(),
        backend,
        protocol_version="itbench_investigation_decision_v2",
    )
    request = runtime.build_request(incident, alerts, run_id=incident.incident_id)
    raw = SimpleNamespace(
        id="v2-response",
        status="completed",
        error=None,
        incomplete_details=None,
        output=[
            SimpleNamespace(
                type="function_call",
                name="request_itbench_tools",
                arguments=json.dumps(
                    {
                        "reason": "inspect",
                        "tool_requests": [
                            {"tool": "itbench_logs", "arguments": {"pattern": None, "limit": 1}}
                        ],
                    }
                ),
            )
        ],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
    )
    response = OpenAIProvider.__new__(OpenAIProvider)._normalize_response(request, raw, 0.0)
    assert set(response.structured_output) == {"decision", "requests", "root_causes", "stop"}
    decision = ITBenchInvestigationDecisionV2.model_validate_json(
        json.dumps(response.structured_output)
    )
    assert decision.decision is ITBenchDecisionType.CALL_TOOLS
    assert decision.requests[0].arguments == {"limit": 1}
    payload = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
    assert "order-service" not in json.dumps(payload)
