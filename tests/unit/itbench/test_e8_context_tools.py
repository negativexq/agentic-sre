"""E8 model-input and public-tool semantic regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import (
    ITBenchExternalToolRegistry,
    ITBenchLiteDataset,
    ITBenchScenario,
    build_external_context_v3,
)
from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.external_contracts import ITBENCH_EXTERNAL_PROTOCOL_V4
from packages.evals.itbench.external_runtime import ExternalInvestigationRuntime
from packages.evals.itbench.incident import build_observable_incident
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend
from packages.investigation.contracts import InvestigationLimits
from packages.provider import FakeModelProvider


def _backend(tmp_path: Path) -> ITBenchSnapshotBackend:
    root = tmp_path / "Scenario-998"
    root.mkdir()
    (root / "alerts.json").write_text(
        json.dumps(
            {
                "alerts": [
                    {
                        "state": "firing",
                        "activeAt": "2025-01-01T00:00:00Z",
                        "labels": {
                            "alertname": "ErrorRate",
                            "service": "frontend",
                            "severity": "warning",
                        },
                        "annotations": {},
                    },
                    {
                        "state": "firing",
                        "activeAt": "2025-01-01T00:01:00Z",
                        "labels": {"alertname": "InfoInhibitor", "severity": "info"},
                        "annotations": {},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    header = "Timestamp\tServiceName\tBody\tValue\tTraceId\n"
    body = json.dumps(
        {
            "kind": "ConfigMap",
            "metadata": {"namespace": "demo", "name": "checkout-config"},
            "data": {"endpoint": "frontend"},
        }
    )
    (root / "objects.tsv").write_text(
        header + f"2025\tfrontend\t{body}\t1\ttrace-1\n", encoding="utf-8"
    )
    event = json.dumps(
        {
            "involvedObject": {
                "kind": "ConfigMap",
                "namespace": "demo",
                "name": "checkout-config",
            },
            "type": "Warning",
            "reason": "ConfigError",
            "message": "invalid endpoint",
        }
    )
    (root / "events.tsv").write_text(
        header + f"2025\tfrontend\t{event}\t1\ttrace-1\n", encoding="utf-8"
    )
    (root / "metrics.tsv").write_text(
        header + "2025\tfrontend\trequest_error_rate\t1\ttrace-1\n"
        "2026\tfrontend\trequest_error_rate\t9\ttrace-2\n",
        encoding="utf-8",
    )
    (root / "logs.tsv").write_text(
        header + "2025\tfrontend\tconnection timeout\t1\ttrace-1\n", encoding="utf-8"
    )
    (root / "traces.tsv").write_text(
        header + "2025\tfrontend\tfailed span\t1\ttrace-1\n", encoding="utf-8"
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
        scenario_id="Scenario-998",
        snapshot_path=str(root),
        evidence_categories=tuple(files),
        evidence_files=files,
    )
    return ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=20_000
    )


def test_context_v3_removes_global_inventory_and_preserves_digest(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    incident, alerts = build_observable_incident(backend)
    context = build_external_context_v3(
        backend,
        incident,
        alerts,
        ITBenchExternalToolRegistry(backend, contract_version="v4").descriptors(),
        candidate_entities=backend.candidate_entities(limit=3),
    )
    payload = json.loads(context)
    assert payload["context_version"] == "itbench_external_context_v3"
    assert "alerts" not in payload
    assert "observable_topology" not in payload
    assert "tool_catalog" not in payload
    assert payload["alert_digest"]["high_signal_alerts"]
    assert len(context) <= 35_000


def test_v4_public_filters_are_semantic_and_contains_is_literal(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend, contract_version="v4")
    alert = registry.invoke("itbench_alert_summary", {"alertname": "ErrorRate", "limit": 10})
    assert alert["matching_count_lower_bound"] == 1
    empty = registry.invoke("itbench_alert_summary", {"alertname": "NoSuchAlert", "limit": 10})
    assert empty["returned_count"] == 0
    entity = registry.invoke(
        "itbench_entity_search",
        {"entity": "demo/ConfigMap/checkout-config", "limit": 10},
    )
    assert entity["returned_count"] == 1
    logs = registry.invoke("itbench_logs", {"contains": "timeout", "limit": 10})
    assert logs["matching_count"] == 1
    regex_like = registry.invoke("itbench_logs", {"contains": "timeout|error", "limit": 10})
    assert regex_like["matching_count"] == 0


def test_v4_entity_context_does_not_claim_unindexed_telemetry_absent(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend, contract_version="v4")
    context = registry.invoke(
        "itbench_entity_context",
        {"entity": "demo/ConfigMap/checkout-config", "limit": 10},
    )
    assert context["telemetry_availability"]["logs"] == "unknown"
    assert context["telemetry_availability"]["traces"] == "unknown"
    assert context["data_quality"]["object_source_count"] == 1


def test_entity_index_and_catalog_are_reused(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    backend.query_entity_context("demo/ConfigMap/checkout-config", 10, include_telemetry=False)
    first = backend.performance_snapshot()
    backend.query_entity_context("demo/ConfigMap/checkout-config", 10, include_telemetry=False)
    second = backend.performance_snapshot()
    assert second["source_file_scans"] == first["source_file_scans"]
    assert second["cache_hits"] > first["cache_hits"]


def test_v4_candidate_state_survives_tool_to_submit_turn(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend, contract_version="v4")
    incident, alerts = build_observable_incident(backend)
    entity = "demo/ConfigMap/checkout-config"
    responses: list[dict[str, Any]] = [
        {
            "decision": "CALL_TOOLS",
            "primary_request": {
                "tool": "itbench_entity_context",
                "arguments": {"entity": entity, "limit": 10},
            },
            "additional_request_2": None,
            "additional_request_3": None,
            "candidate_update_1": {
                "entity": entity,
                "status": "ACTIVE",
                "supporting_refs": [],
                "contradicting_refs": [],
                "last_tested_question": "Does this config explain the symptom?",
            },
            "candidate_update_2": None,
            "candidate_update_3": None,
            "primary_root_cause": None,
            "additional_root_cause_2": None,
            "additional_root_cause_3": None,
            "stop": None,
        },
        {
            "decision": "SUBMIT_DIAGNOSIS",
            "primary_request": None,
            "additional_request_2": None,
            "additional_request_3": None,
            "candidate_update_1": {
                "entity": entity,
                "status": "SUPPORTED",
                "supporting_refs": ["E001"],
                "contradicting_refs": [],
                "last_tested_question": "Does this config explain the symptom?",
            },
            "candidate_update_2": None,
            "candidate_update_3": None,
            "primary_root_cause": {
                "entity": entity,
                "causal_summary": "observable configuration evidence",
                "evidence_refs": ["E001"],
            },
            "additional_root_cause_2": None,
            "additional_root_cause_3": None,
            "stop": None,
        },
    ]
    provider = FakeModelProvider(responses)
    result = ExternalInvestigationRuntime(
        provider,
        registry.investigation_registry(),
        backend,
        protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V4,
        limits=InvestigationLimits(
            max_model_calls=2, max_tool_calls=3, max_agent_turns=2, max_wall_time_seconds=60
        ),
    ).run(incident, alerts)
    assert result.terminal == "SUBMIT_DIAGNOSIS"
    assert result.turns[0]["case_state"]["active_candidates"] == [entity]
    assert result.turns[1]["root_cause_count"] == 1


def test_v4_subsumed_fixed_window_query_does_not_execute_again(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend, contract_version="v4")
    incident, alerts = build_observable_incident(backend)
    entity = "demo/ConfigMap/checkout-config"

    def request(limit: int) -> dict[str, Any]:
        return {
            "decision": "CALL_TOOLS",
            "primary_request": {
                "tool": "itbench_entity_context",
                "arguments": {"entity": entity, "limit": limit},
            },
            "additional_request_2": None,
            "additional_request_3": None,
            "candidate_update_1": None,
            "candidate_update_2": None,
            "candidate_update_3": None,
            "primary_root_cause": None,
            "additional_root_cause_2": None,
            "additional_root_cause_3": None,
            "stop": None,
        }

    stop: dict[str, Any] = {
        "decision": "STOP",
        "primary_request": None,
        "additional_request_2": None,
        "additional_request_3": None,
        "candidate_update_1": None,
        "candidate_update_2": None,
        "candidate_update_3": None,
        "primary_root_cause": None,
        "additional_root_cause_2": None,
        "additional_root_cause_3": None,
        "stop": {
            "stop_reason": "insufficient_evidence",
            "evidence_categories_considered": ["k8s_objects"],
            "entities_considered": [entity],
            "missing_evidence_categories": ["metrics"],
        },
    }
    result = ExternalInvestigationRuntime(
        FakeModelProvider([request(20), request(10), stop]),
        registry.investigation_registry(),
        backend,
        protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V4,
        limits=InvestigationLimits(
            max_model_calls=3, max_tool_calls=3, max_agent_turns=3, max_wall_time_seconds=60
        ),
    ).run(incident, alerts)
    assert result.terminal == "STOP"
    assert result.usage["semantic_tool_executions"] == 1
    assert result.usage["subsumed_duplicate_requests"] == 1
