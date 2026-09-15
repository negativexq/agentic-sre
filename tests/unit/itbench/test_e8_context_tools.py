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
from packages.evals.itbench.external_context import _fit_alert_digest
from packages.evals.itbench.external_contracts import ITBENCH_EXTERNAL_PROTOCOL_V4
from packages.evals.itbench.external_runtime import (
    ExternalInvestigationRuntime,
    _associate_candidate_evidence,
    _format_external_observation,
)
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


def test_v4_typed_filters_reach_backend_semantics(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    registry = ITBenchExternalToolRegistry(backend, contract_version="v4")

    service = registry.invoke("itbench_logs", {"service": "frontend", "limit": 10})
    assert service["matching_count"] == 1
    assert (
        registry.invoke("itbench_logs", {"service": "payments", "limit": 10})["matching_count"] == 0
    )

    alias = registry.invoke("itbench_alert_summary", {"service": "frontend", "limit": 10})
    assert alias["returned_count"] == 1
    assert (
        registry.invoke("itbench_alert_summary", {"service": "payments", "limit": 10})[
            "returned_count"
        ]
        == 0
    )

    events = registry.invoke(
        "itbench_kubernetes_events", {"reason": "ConfigError", "type": "Warning", "limit": 5}
    )
    assert events["matching_count"] == 1
    assert (
        registry.invoke(
            "itbench_kubernetes_events", {"reason": "Other", "type": "Warning", "limit": 5}
        )["matching_count"]
        == 0
    )


def test_alert_digest_semantic_packing_preserves_each_priority_section() -> None:
    digest = {
        "high_signal_alerts": [
            {"alertname": f"signal-{index}", "message": "x" * 240} for index in range(20)
        ],
        "affected_services": [f"service-{index}" for index in range(20)],
        "affected_namespaces": [f"namespace-{index}" for index in range(20)],
        "alert_counts_by_name": {f"alert-{index}": index + 1 for index in range(20)},
        "background": {f"background-{index}": index + 1 for index in range(20)},
    }
    packed = _fit_alert_digest(digest, 1_000)
    encoded = json.dumps(packed, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 1_000
    assert "signal-0" in encoded
    assert "service-0" in encoded
    assert "namespace-0" in encoded
    assert "alert-0" in encoded
    assert "background-0" in encoded
    assert packed != {"truncated": True}


def test_evidence_dict_packing_is_bounded_without_original_summary() -> None:
    observation = {
        "entity": "demo/Pod/app",
        "metric_anomalies": {
            f"metric-{index}": {"incident": "x" * 240, "baseline": index} for index in range(20)
        },
        "object_state": {"phase": "Running", "ready": True},
    }
    formatted = _format_external_observation(
        "itbench_entity_context", observation, semantic_v4=True
    )
    assert len(formatted) <= 120 + 220 + 500 + 700 + 40
    assert '"summary"' not in formatted
    assert "metric-0" in formatted
    assert '"entity":"demo/Pod/app"' in formatted


def test_rejected_candidate_is_not_reactivated_by_tool_evidence() -> None:
    state: dict[str, Any] = {
        "active_candidates": [],
        "supported_candidates": [],
        "rejected_candidates": ["demo/Pod/app"],
    }
    _associate_candidate_evidence(state, "demo/Pod/app", "E001")
    assert state["active_candidates"] == []
    assert state["rejected_candidates"] == ["demo/Pod/app"]
    assert state["evidence_by_candidate"]["demo/Pod/app"] == ["E001"]


def test_tool_candidate_association_cannot_bypass_three_active_limit() -> None:
    state: dict[str, Any] = {
        "active_candidates": ["demo/Pod/a", "demo/Pod/b", "demo/Pod/c"],
        "supported_candidates": [],
        "rejected_candidates": [],
    }
    _associate_candidate_evidence(state, "demo/Pod/d", "E004")
    assert len(state["active_candidates"]) == 3
    assert state["evidence_by_candidate"]["demo/Pod/d"] == ["E004"]
    assert state["candidate_association_skips"][0]["reason"] == "ACTIVE_CANDIDATE_LIMIT"


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
