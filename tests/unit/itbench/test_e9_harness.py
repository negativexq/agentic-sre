"""Offline tests for the E9 harness-owned control plane."""

import json
from pathlib import Path
from typing import cast

from packages.evals.itbench import (
    ITBenchEvidenceCategory,
    ITBenchLiteDataset,
    ITBenchScenario,
    ITBenchSnapshotBackend,
    build_observable_incident,
)
from packages.evals.itbench.e9_fsm import E9FSM, E9Phase
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import E9InvestigationRuntime
from packages.evals.itbench.external_contracts import (
    E9Action,
    ITBenchInvestigationDecisionV5,
)
from packages.provider import FakeModelProvider
from packages.provider.openai import OpenAIProvider


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
    raw_files = {
        ITBenchEvidenceCategory.K8S_EVENTS: "k8s_events_raw.tsv",
        ITBenchEvidenceCategory.K8S_OBJECTS: "k8s_objects_raw.tsv",
        ITBenchEvidenceCategory.LOGS: "otel_logs_raw.tsv",
        ITBenchEvidenceCategory.TRACES: "otel_traces_raw.tsv",
    }
    for filename in raw_files.values():
        (scenario_dir / filename).write_text(header + f"2025\tfrontend\t{body}\n", encoding="utf-8")
    (scenario_dir / "metric.tsv").write_text(
        header + "2025\tfrontend\tdegraded\n", encoding="utf-8"
    )
    evidence_files: dict[ITBenchEvidenceCategory, tuple[str, ...]] = {
        ITBenchEvidenceCategory.ALERTS: ("alerts.json",),
        ITBenchEvidenceCategory.METRICS: ("metric.tsv",),
        **{category: (filename,) for category, filename in raw_files.items()},
    }
    return ITBenchScenario(
        scenario_id="Scenario-1",
        snapshot_path=str(scenario_dir),
        evidence_categories=tuple(ITBenchEvidenceCategory),
        evidence_files=evidence_files,
    )


def _runtime(tmp_path: Path, responses: list[dict[str, object]]) -> E9InvestigationRuntime:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    return E9InvestigationRuntime(FakeModelProvider(responses), backend)


def test_v5_valid_trajectory_owns_identity_and_evidence(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": "test candidate"},
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "ENTITY_CONTEXT",
                "rationale": "inspect state",
            },
            {"action": "SUBMIT", "targets": ["C001"]},
        ],
    )
    result = runtime.run(*build_observable_incident(runtime.backend))
    assert result["terminal"] == "SUBMIT"
    assert result["submitted_entities"] == ["otel-demo/Service/frontend"]
    assert result["submitted_evidence_refs"] == ["E001"]
    assert result["execution_id"] == "ITB-E9"
    assert result["case_id"] == "ITB-E9:Scenario-1"
    assert result["usage"]["model_steps"] == 3
    assert result["safety"]["ground_truth_exposure"] == 0


def test_invalid_safe_action_is_rejected_then_recovered(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {"action": "HYPOTHESIZE", "target": "C999", "rationale": "bad handle"},
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": "retry"},
            {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
            {"action": "SUBMIT", "targets": ["C001"]},
        ],
    )
    result = runtime.run(*build_observable_incident(runtime.backend))
    assert result["terminal"] == "SUBMIT"
    assert result["usage"]["action_rejections"] == 1
    assert result["usage"]["recovered_action_rejections"] == 1
    assert result["case_state"]["current_phase"] == "CONCLUDE"


def test_memory_replay_reconstructs_projection(tmp_path: Path) -> None:
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    memory.set_candidate_status(turn=1, handle="C001", status="ACTIVE")
    memory.add_evidence(
        turn=2,
        handle="C001",
        operation="ENTITY_CONTEXT",
        category="entity_context",
        summary={"state": "ready"},
    )
    path = tmp_path / "memory.json"
    memory.persist(path)
    replayed = E9CaseMemory.replay(json.loads(path.read_text(encoding="utf-8")))
    assert replayed.projection() == memory.projection()
    assert replayed.resolve("C001") == "otel-demo/Service/frontend"


def test_rejected_candidate_cannot_reactivate_and_active_limit_is_centralized() -> None:
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(
        tuple({"canonical": f"otel-demo/Service/service-{index}"} for index in range(1, 6))
    )
    memory.set_candidate_status(turn=1, handle="C001", status="REJECTED")
    try:
        memory.set_candidate_status(turn=2, handle="C001", status="ACTIVE")
    except ValueError as error:
        assert "reactivated" in str(error)
    else:
        raise AssertionError("rejected candidate was reactivated")
    for index in range(1, 4):
        memory.set_candidate_status(turn=3, handle=f"C00{index + 1}", status="ACTIVE")
    try:
        memory.set_candidate_status(turn=4, handle="C005", status="ACTIVE")
    except ValueError as error:
        assert "maximum active" in str(error)
    else:
        raise AssertionError("active candidate limit was bypassed")
    assert len(memory.projection()["active_candidates"]) == 3


def test_fsm_final_turn_exposes_only_terminal_actions() -> None:
    fsm = E9FSM()
    assert fsm.phase is E9Phase.OBSERVE
    assert fsm.valid_actions(has_hypothesis=True, evidence_count=1, final_turn=True) == (
        "SUBMIT",
        "STOP",
    )


def test_v5_zero_cardinality_is_unavailable_or_rejected() -> None:
    try:
        ITBenchInvestigationDecisionV5.model_validate({"action": "SUBMIT", "targets": []})
    except ValueError:
        pass
    else:
        raise AssertionError("empty SUBMIT target set was accepted")
    try:
        ITBenchInvestigationDecisionV5.model_validate({"action": "INVESTIGATE", "target": "C001"})
    except ValueError:
        pass
    else:
        raise AssertionError("INVESTIGATE without an operation was accepted")
    stop = ITBenchInvestigationDecisionV5.model_validate(
        {"action": E9Action.STOP, "stop_reason": "insufficient evidence"}
    )
    assert stop.stop_reason == "insufficient evidence"


def test_v5_provider_schema_is_ref_free_and_has_terminal_split(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [])
    incident, alerts = build_observable_incident(runtime.backend)
    request, _ = runtime.build_request(
        incident,
        alerts,
        E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1"),
        E9FSM(),
        run_id=incident.incident_id,
        turn=1,
    )
    parameters = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
    assert all("$ref" not in json.dumps(item) for item in parameters["tools"])
    assert {item["name"] for item in parameters["tools"]} == {
        "request_itbench_tools",
        "submit_itbench_diagnosis",
        "stop_itbench_investigation",
    }
    final_request, _ = runtime.build_request(
        incident,
        alerts,
        E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1"),
        E9FSM(),
        run_id=incident.incident_id,
        turn=12,
    )
    final_parameters = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(final_request)
    assert {item["name"] for item in final_parameters["tools"]} == {
        "submit_itbench_diagnosis",
        "stop_itbench_investigation",
    }
