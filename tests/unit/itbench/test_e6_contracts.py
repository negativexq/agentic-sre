"""E6 contract, metric, and process-boundary regressions."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from packages.evals.itbench import (
    ITBENCH_EXTERNAL_PROTOCOL_V3,
    ITBenchEvidenceCategory,
    ITBenchExternalToolRegistry,
    ITBenchInvestigationDecisionV2,
    ITBenchInvestigationDecisionV3,
    ITBenchLiteDataset,
    ITBenchScenario,
)
from packages.evals.itbench.external_runtime import ExternalInvestigationRuntime
from packages.evals.itbench.incident import build_observable_incident
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend
from packages.investigation.contracts import InvestigationLimits
from packages.provider import FakeModelProvider, ModelRequest, OpenAIProvider


def _backend(tmp_path: Path) -> ITBenchSnapshotBackend:
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
    body = json.dumps(
        {"kind": "ConfigMap", "metadata": {"namespace": "demo", "name": "checkout-config"}}
    )
    (root / "objects.tsv").write_text(
        header + f"2025\tfrontend\t{body}\t1\ttrace-1\n", encoding="utf-8"
    )
    (root / "events.tsv").write_text(
        header + f"2025\tfrontend\t{body}\t1\ttrace-1\n", encoding="utf-8"
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
    scenario = ITBenchScenario(
        scenario_id="Scenario-999",
        snapshot_path=str(root),
        evidence_categories=tuple(files),
        evidence_files=files,
    )
    return ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=20_000
    )


def _request(
    tmp_path: Path, protocol: str = ITBENCH_EXTERNAL_PROTOCOL_V3
) -> tuple[ITBenchSnapshotBackend, ModelRequest]:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend)
    incident, alerts = build_observable_incident(backend)
    runtime = ExternalInvestigationRuntime(
        FakeModelProvider([]),
        registry.investigation_registry(),
        backend,
        protocol_version=protocol,
        limits=InvestigationLimits(
            max_model_calls=2, max_tool_calls=12, max_agent_turns=2, max_wall_time_seconds=180
        ),
    )
    return backend, runtime.build_request(incident, alerts, run_id=incident.incident_id)


def _raw(function: str, arguments: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        id="e6-response",
        status="completed",
        error=None,
        incomplete_details=None,
        output=[
            SimpleNamespace(type="function_call", name=function, arguments=json.dumps(arguments))
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def test_provider_wire_and_local_cardinality_have_one_truth(tmp_path: Path) -> None:
    _backend_instance, request = _request(tmp_path)
    for count in range(1, 7):
        args: dict[str, object] = {
            "reason": "inspect",
            "tool_requests": [
                {
                    "tool": "itbench_logs",
                    "arguments": {"pattern": None, "limit": 1},
                }
                for _ in range(count)
            ],
        }
        normalized = OpenAIProvider.__new__(OpenAIProvider)._normalize_response(
            request, _raw("request_itbench_tools", args), 0.0
        )
        assert ITBenchInvestigationDecisionV3.model_validate_json(
            json.dumps(normalized.structured_output)
        )


def test_v2_historical_hidden_bound_is_reproduced(tmp_path: Path) -> None:
    _backend_instance, request = _request(tmp_path, "itbench_investigation_decision_v2")
    args: dict[str, object] = {
        "reason": "inspect",
        "tool_requests": [
            {"tool": "itbench_logs", "arguments": {"pattern": None, "limit": 1}} for _ in range(4)
        ],
    }
    normalized = OpenAIProvider.__new__(OpenAIProvider)._normalize_response(
        request, _raw("request_itbench_tools", args), 0.0
    )
    with pytest.raises(ValidationError):
        ITBenchInvestigationDecisionV2.model_validate(normalized.structured_output)


def test_metric_analysis_is_one_backend_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend)
    original = backend._iter_records
    scans = 0

    def counted(category: ITBenchEvidenceCategory) -> Iterator[dict[str, object]]:
        nonlocal scans
        if category.value == "metrics":
            scans += 1
        return original(category)

    monkeypatch.setattr(backend, "_iter_records", counted)
    result = registry.invoke("itbench_metric_analysis", {"service": "frontend", "limit": 1})
    assert result["aggregate"]["count"] == 2
    assert result["sample_count"] == 1
    assert scans == 1


def test_topology_public_filters_are_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend)
    seen: dict[str, object] = {}

    def topology(**kwargs: object) -> tuple[dict[str, object], ...]:
        seen.update(kwargs)
        return (
            {
                "source": "demo/Service/frontend",
                "target": "demo/Pod/frontend-1",
                "relationship": "selector",
            },
        )

    monkeypatch.setattr(backend, "topology", topology)
    result = registry.invoke(
        "itbench_topology",
        {
            "entity": "demo/Service/frontend",
            "namespace": "demo",
            "kind": "Service",
            "pattern": "frontend",
            "relationship": "selector",
            "limit": 1,
        },
    )
    assert result["returned_count"] == 1
    assert seen == {
        "entity": "demo/Service/frontend",
        "namespace": "demo",
        "kind": "Service",
        "pattern": "frontend",
        "relationship": "selector",
        "limit": 1,
    }


def test_entity_context_filters_topology_before_bounding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend(tmp_path)
    calls: list[dict[str, object]] = []

    def topology(**kwargs: object) -> tuple[dict[str, object], ...]:
        calls.append(kwargs)
        return (
            {
                "source": "demo/ConfigMap/checkout-config",
                "target": "demo/Pod/late",
                "relationship": "configuration_reference",
            },
        )

    monkeypatch.setattr(backend, "topology", topology)
    result = backend.query_entity_context("demo/ConfigMap/checkout-config", 1)
    assert result["topology"]
    assert calls == [{"entity": "demo/ConfigMap/checkout-config", "limit": 1}]
