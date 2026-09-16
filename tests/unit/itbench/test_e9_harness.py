"""Offline tests for the E9 harness-owned control plane."""

import json
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import (
    ITBenchEvidenceCategory,
    ITBenchLiteDataset,
    ITBenchScenario,
    ITBenchSnapshotBackend,
    build_observable_incident,
)
from packages.evals.itbench.e9_context import E9ContextPlanner, _fit_alert_digest
from packages.evals.itbench.e9_control import control_surface
from packages.evals.itbench.e9_fsm import E9FSM
from packages.evals.itbench.e9_identity import (
    E9IdentityMismatch,
    collect_e9_identity,
    validate_e9_identity,
)
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_packing import bounded_pack
from packages.evals.itbench.e9_runtime import E9InvestigationRuntime
from packages.evals.itbench.e9_semantic import (
    E9_SEMANTIC_OPERATIONS,
    E9SemanticOperations,
    SemanticCapabilityResolver,
    _temporal_assessment,
)
from packages.evals.itbench.external_contracts import (
    E9Action,
    ITBenchInvestigationDecisionV5,
)
from packages.provider import FakeModelProvider
from packages.provider.openai import (
    OpenAIProvider,
    _itbench_v5_decision_function_schemas,
    _json_schema_error,
)


def _semantic_budget_state(
    *,
    used: int,
    current: str = "C001",
    completed: tuple[tuple[str, str], ...] = (),
    evidence: bool = False,
) -> dict[str, Any]:
    discovered = {
        f"otel-demo/Service/{handle.lower()}": {
            "handle": handle,
            "canonical": f"otel-demo/Service/{handle.lower()}",
            "metadata": {},
            "discovered_turn": 0,
        }
        for handle in ("C001", "C002")
    }
    return {
        "current_phase": "VERIFY",
        "current_hypothesis": {"entity_handle": current, "rationale": "test"},
        "alternative_candidates": [],
        "discovered_entities": discovered,
        "operations_already_run": [
            {"entity_handle": handle, "operation": operation} for handle, operation in completed
        ],
        "semantic_actions_used": used,
        "consecutive_rejections": 0,
        "evidence": (
            {"E001": {"entity_handle": current, "operation": "ENTITY_CONTEXT"}} if evidence else {}
        ),
    }


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


def test_candidate_history_allows_reconsideration_without_active_overflow() -> None:
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(
        tuple({"canonical": f"otel-demo/Service/service-{index}"} for index in range(1, 6))
    )
    memory.set_candidate_status(turn=1, handle="C001", status="REJECTED")
    memory.set_candidate_status(turn=2, handle="C001", status="ACTIVE")
    for index in range(1, 5):
        memory.set_candidate_status(turn=3, handle=f"C00{index + 1}", status="ACTIVE")
    assert memory.projection()["candidate_state"]["C005"]["status"] == "ACTIVE"


def test_five_hypothesis_revisions_are_reversible_and_replayable() -> None:
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(
        tuple({"canonical": f"otel-demo/Service/service-{index}"} for index in range(1, 6))
    )
    for turn in range(1, 6):
        event_type = "HYPOTHESIS_PROPOSED" if turn == 1 else "HYPOTHESIS_REVISED"
        payload: dict[str, Any] = (
            {"entity_handle": f"C00{turn}", "rationale": "bounded test"}
            if turn == 1
            else {"hypothesis": {"entity_handle": f"C00{turn}", "rationale": "revised"}}
        )
        memory.append(event_type, turn, payload)
    replay = E9CaseMemory.replay(
        {
            "execution_id": memory.execution_id,
            "scenario_id": memory.scenario_id,
            "case_id": memory.case_id,
            "events": [event.as_dict() for event in memory.events],
        }
    )
    assert memory.state["current_hypothesis"]["entity_handle"] == "C005"
    assert len(memory.state["hypothesis_history"]) == 5
    assert replay.projection() == memory.projection()


def test_structured_packer_preserves_late_fields_without_string_prefix() -> None:
    value = {
        "noise": ["x" * 400] * 8,
        "critical_field": "must survive",
        "data_quality": {"ok": True},
    }
    packed = bounded_pack(value, 500)
    encoded = json.dumps(packed, ensure_ascii=False)
    assert len(encoded) <= 500
    assert isinstance(packed, dict)
    assert not isinstance(packed.get("summary"), str)
    assert packed.get("section_truncated") is True


def test_temporal_assessment_is_not_timestamp_sign_only() -> None:
    assert _temporal_assessment(-120, "unknown") == ("NEAR_ONSET", "SUPPORTS")
    assert _temporal_assessment(1200, "failure") == ("AFTER", "INCONCLUSIVE")
    assert _temporal_assessment(-1200, "unknown") == ("BEFORE", "INCONCLUSIVE")


def test_newly_discovered_candidate_is_visible_and_targetable() -> None:
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/first"},), turn=1)
    memory.append("HYPOTHESIS_PROPOSED", 2, {"entity_handle": "C001", "rationale": "seed"})
    memory.discover_entities(({"canonical": "otel-demo/ConfigMap/related"},), turn=3)
    surface = control_surface(
        memory.state, turn=3, max_steps=12, max_rejections=2, semantic_limit=24
    )
    assert "C002" in surface.target_handles
    assert "C002" in memory.projection()["discovered_entities"][-1]["handle"]


def test_alert_digest_keeps_multiple_sections_when_bounded() -> None:
    digest = _fit_alert_digest(
        {
            "high_signal_alerts": [
                {"alertname": "Failure", "detail": "x" * 600} for _ in range(30)
            ],
            "affected_services": ["checkout", "frontend"],
            "affected_namespaces": ["otel-demo"],
            "alert_counts_by_name": {"Failure": 30, "Watchdog": 2},
            "background": {"Watchdog": 2},
        },
        900,
    )
    assert "high_signal_alerts" in digest
    assert "affected_services" in digest
    assert "alert_counts_by_name" in digest
    assert json.dumps(digest, ensure_ascii=False).__len__() <= 900


def test_fsm_final_turn_exposes_only_terminal_actions() -> None:
    fsm = E9FSM()
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
        "stop_itbench_investigation",
    }


def test_v5_provider_surface_matches_control_policy(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [])
    incident, alerts = build_observable_incident(runtime.backend)
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    request, _ = runtime.build_request(
        incident, alerts, memory, run_id=incident.incident_id, turn=1
    )
    parameters = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
    request_schema = next(
        item["parameters"]
        for item in parameters["tools"]
        if item["name"] == "request_itbench_tools"
    )
    assert len(request_schema["anyOf"]) == 1
    assert request_schema["anyOf"][0]["properties"]["action"]["enum"] == ["HYPOTHESIZE"]
    memory.append(
        "HYPOTHESIS_PROPOSED",
        1,
        {"entity_handle": "C001", "rationale": "verify"},
    )
    memory.add_evidence(
        turn=2, handle="C001", operation="ENTITY_CONTEXT", category="entity", summary={"ok": True}
    )
    final_request, _ = runtime.build_request(
        incident, alerts, memory, run_id=incident.incident_id, turn=12
    )
    final_parameters = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(final_request)
    assert {item["name"] for item in final_parameters["tools"]} == {
        "submit_itbench_diagnosis",
        "stop_itbench_investigation",
    }


def test_rejection_feedback_is_visible_in_next_context(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    incident, alerts = build_observable_incident(backend)
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    memory.append(
        "ACTION_REJECTED",
        2,
        {
            "attempted_action": "INVESTIGATE",
            "attempted_target": "C001",
            "attempted_operation": "METRIC_ANOMALIES",
            "code": "DUPLICATE_OPERATION",
            "reason": "operation already completed",
            "valid_actions": ["INVESTIGATE", "REVISE", "STOP"],
            "valid_operations": ["SPEC_ANALYSIS"],
        },
    )
    context, _ = E9ContextPlanner().plan(
        backend, incident, alerts, memory, None, turn=3, max_steps=12, semantic_limit=24
    )
    assert "DUPLICATE_OPERATION" in context
    assert "METRIC_ANOMALIES" in context
    assert "operation already completed" in context
    assert "SPEC_ANALYSIS" in context


def test_replay_equality_after_rejection_recovery_and_terminal() -> None:
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    memory.append(
        "ACTION_REJECTED",
        1,
        {"code": "UNKNOWN_CANDIDATE", "reason": "unknown", "valid_actions": ["HYPOTHESIZE"]},
    )
    memory.append("ACTION_ACCEPTED", 2, {"action": "HYPOTHESIZE", "target": "C001"})
    memory.set_candidate_status(turn=2, handle="C001", status="ACTIVE")
    memory.append("HYPOTHESIS_PROPOSED", 2, {"entity_handle": "C001", "rationale": "test"})
    memory.add_evidence(
        turn=3, handle="C001", operation="ENTITY_CONTEXT", category="entity", summary={"ok": True}
    )
    memory.append("ACTION_ACCEPTED", 4, {"action": "SUBMIT", "targets": ["C001"]})
    memory.append("DIAGNOSIS_SUBMITTED", 4, {"targets": ["C001"]})
    payload = {
        "execution_id": memory.execution_id,
        "scenario_id": memory.scenario_id,
        "case_id": memory.case_id,
        "events": [event.as_dict() for event in memory.events],
    }
    assert E9CaseMemory.replay(payload).projection() == memory.projection()


def test_runtime_identity_collector_and_fail_closed_validation(tmp_path: Path) -> None:
    source = tmp_path / "identity.txt"
    source.write_text("stable", encoding="utf-8")
    actual = collect_e9_identity(Path.cwd(), ("packages/evals/itbench/e9_control.py",))
    clean_actual = {**actual, "relevant_worktree_dirty": False}
    validate_e9_identity(clean_actual, {"runtime_identity": clean_actual})
    mismatched = {"runtime_identity": {**clean_actual, "bundle_sha256": "wrong"}}
    try:
        validate_e9_identity(actual, mismatched)
    except E9IdentityMismatch:
        pass
    else:
        raise AssertionError("identity mismatch was not rejected")


def test_v5_function_call_is_dispatched_by_response_normalizer(tmp_path: Path) -> None:
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
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "name": "stop_itbench_investigation",
                "arguments": json.dumps({"stop_reason": "insufficient evidence"}),
            }
        ],
    }
    response = OpenAIProvider.__new__(OpenAIProvider)._normalize_response(request, raw, 0.0)
    assert response.structured_output["action"] == "STOP"


def test_rejection_feedback_is_visible_and_replayable() -> None:
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.append(
        "ACTION_REJECTED",
        2,
        {
            "attempted_action": "INVESTIGATE",
            "attempted_target": "C001",
            "attempted_operation": "METRIC_ANOMALIES",
            "code": "DUPLICATE_OPERATION",
            "reason": "operation already completed",
            "valid_actions": ["INVESTIGATE", "REVISE", "STOP"],
            "valid_operations": ["SPEC_ANALYSIS"],
        },
    )
    projection = memory.projection()
    assert projection["last_rejection"]["code"] == "DUPLICATE_OPERATION"
    assert projection["last_rejection"]["attempted_operation"] == "METRIC_ANOMALIES"
    replayed = E9CaseMemory.replay(
        {
            "execution_id": memory.execution_id,
            "scenario_id": memory.scenario_id,
            "case_id": memory.case_id,
            "events": [event.as_dict() for event in memory.events],
        }
    )
    assert replayed.projection() == projection


def test_control_surface_is_dynamic_and_removes_completed_operation() -> None:
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    first = control_surface(memory.state, turn=1, max_steps=12, max_rejections=2, semantic_limit=24)
    assert "HYPOTHESIZE" in first.actions
    assert first.operations == ()
    memory.append(
        "OPERATION_REQUESTED", 1, {"entity_handle": None, "operation": "RECENT_CHANGE_ANALYSIS"}
    )
    second = control_surface(
        memory.state, turn=2, max_steps=12, max_rejections=2, semantic_limit=24
    )
    assert second.operations == ()


def _budget_surface(
    state: dict[str, Any], *, limit: int = 3, available: tuple[str, ...] | None = None
) -> Any:
    return control_surface(
        state,
        turn=2,
        max_steps=12,
        max_rejections=2,
        semantic_limit=limit,
        available_operations=available,
    )


def test_semantic_budget_below_limit_keeps_investigation_visible() -> None:
    surface = _budget_surface(_semantic_budget_state(used=2), available=("ENTITY_CONTEXT",))
    assert surface.operations == ("ENTITY_CONTEXT",)
    assert "INVESTIGATE" in surface.actions
    assert surface.capabilities()["INVESTIGATE"] == {
        "targets": ("C001",),
        "operations": ("ENTITY_CONTEXT",),
    }


def test_semantic_budget_at_limit_removes_investigation() -> None:
    surface = _budget_surface(
        _semantic_budget_state(used=3), available=("ENTITY_CONTEXT", "EVENT_ANALYSIS")
    )
    assert surface.operations == ()
    assert "INVESTIGATE" not in surface.actions
    assert "INVESTIGATE" not in surface.capabilities()


def test_semantic_budget_over_limit_removes_investigation() -> None:
    surface = _budget_surface(
        _semantic_budget_state(used=4), available=("ENTITY_CONTEXT", "SPEC_ANALYSIS")
    )
    assert surface.operations == ()
    assert "INVESTIGATE" not in surface.actions


def test_capability_availability_intersects_policy_and_duplicate_filters() -> None:
    surface = _budget_surface(
        _semantic_budget_state(used=0, completed=(("C001", "ENTITY_CONTEXT"),)),
        available=("ENTITY_CONTEXT", "SPEC_ANALYSIS"),
    )
    assert surface.operations == ("SPEC_ANALYSIS",)


def test_duplicate_suppression_remains_target_scoped_after_revision() -> None:
    surface = _budget_surface(
        _semantic_budget_state(used=1, current="C002", completed=(("C001", "ENTITY_CONTEXT"),)),
        available=("ENTITY_CONTEXT",),
    )
    assert surface.operations == ("ENTITY_CONTEXT",)
    assert surface.capabilities()["INVESTIGATE"]["targets"] == ("C002",)


def test_submit_remains_available_at_semantic_budget_limit_with_evidence() -> None:
    surface = _budget_surface(
        _semantic_budget_state(used=3, evidence=True),
        available=("ENTITY_CONTEXT", "EVENT_ANALYSIS"),
    )
    assert surface.operations == ()
    assert surface.actions == ("REVISE", "SUBMIT", "STOP")
    assert surface.capabilities()["SUBMIT"]["targets"] == ("C001",)


def test_no_evidence_at_semantic_budget_limit_allows_only_revision_or_stop() -> None:
    surface = _budget_surface(
        _semantic_budget_state(used=3),
        available=("ENTITY_CONTEXT", "EVENT_ANALYSIS"),
    )
    assert surface.actions == ("REVISE", "STOP")
    assert "SUBMIT" not in surface.actions
    assert "INVESTIGATE" not in surface.actions


def test_exhausted_budget_provider_schema_has_no_investigate_branch() -> None:
    surface = _budget_surface(_semantic_budget_state(used=3), available=("ENTITY_CONTEXT",))
    schemas = _itbench_v5_decision_function_schemas(
        allowed_actions=tuple(
            action for action in surface.actions if action not in {"SUBMIT", "STOP"}
        ),
        allowed_operations=surface.operations,
        allowed_targets=surface.target_handles,
        allowed_action_capabilities=surface.capabilities(),
    )
    request_schema = schemas["request_itbench_tools"]
    assert all(
        "INVESTIGATE" not in branch["properties"]["action"].get("enum", [])
        for branch in request_schema["anyOf"]
    )


def test_exhausted_budget_context_has_no_investigation_surface(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    incident, alerts = build_observable_incident(backend)
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    memory.append("HYPOTHESIS_PROPOSED", 1, {"entity_handle": "C001", "rationale": "test"})
    memory.state["semantic_actions_used"] = 3
    context, _ = E9ContextPlanner().plan(
        backend, incident, alerts, memory, None, turn=2, max_steps=12, semantic_limit=3
    )
    payload = json.loads(context)
    assert payload["workflow"]["semantic_actions_remaining"] == 0
    assert "INVESTIGATE" not in payload["workflow"]["actions"]


def test_runtime_rejects_injected_investigation_after_budget_exhaustion(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": "test"},
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "ENTITY_CONTEXT",
            },
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "ENTITY_CONTEXT",
            },
            {"action": "STOP", "stop_reason": "budget regression test"},
        ],
    )
    runtime.limits = type(runtime.limits)(max_tool_calls=1)
    result = runtime.run(*build_observable_incident(runtime.backend))
    assert result["terminal"] == "STOP"
    assert result["usage"]["semantic_actions_executed"] == 1
    assert result["usage"]["action_rejections"] == 1


def test_every_registered_semantic_operation_has_a_real_bounded_executor(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)
    backend = ITBenchSnapshotBackend(
        cast(ITBenchLiteDataset, object()), scenario, max_rows=5, max_bytes=10_000
    )
    incident, _ = build_observable_incident(backend)
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    operations = E9SemanticOperations(backend, memory, incident)
    for index, operation in enumerate(E9_SEMANTIC_OPERATIONS, start=1):
        target = (
            None
            if operation in {"INCIDENT_OVERVIEW", "ALERT_ANALYSIS", "TOPOLOGY_ANALYSIS"}
            else "C001"
        )
        result = operations.execute(operation, target, index)
        assert result["operation"] == operation
        assert result["evidence_ref"].startswith("E")
        assert len(json.dumps(result["summary"], default=str)) <= 5_500


def test_provider_target_enum_rejects_unknown_handle_offline(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [])
    incident, alerts = build_observable_incident(runtime.backend)
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    request, _ = runtime.build_request(
        incident, alerts, memory, run_id=incident.incident_id, turn=1
    )
    parameters = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
    schema = next(
        item["parameters"]
        for item in parameters["tools"]
        if item["name"] == "request_itbench_tools"
    )
    target_schema = schema["anyOf"][0]["properties"]["target"]
    assert target_schema["enum"] == ["C001"]
    invalid: dict[str, object] = {
        "action": "HYPOTHESIZE",
        "target": "C999",
        "targets": [],
        "operation": None,
        "rationale": None,
    }
    assert _json_schema_error(invalid, schema) is not None


def test_capability_resolver_removes_known_dead_operations(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [])
    incident, _ = build_observable_incident(runtime.backend)
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    resolver = SemanticCapabilityResolver(runtime.backend, memory, incident)
    available = resolver.available_operations(phase="OBSERVE")
    assert "RECENT_CHANGE_ANALYSIS" not in available


def test_submit_requires_candidate_associated_evidence() -> None:
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(
        (
            {"canonical": "otel-demo/Service/one"},
            {"canonical": "otel-demo/Service/two"},
        )
    )
    memory.append("HYPOTHESIS_PROPOSED", 1, {"entity_handle": "C002", "rationale": "test"})
    memory.add_evidence(
        turn=2,
        handle=None,
        operation="ALERT_ANALYSIS",
        category="alerts",
        summary={"alert": "global"},
    )
    assert not E9InvestigationRuntime._submit_ready(memory, ["C002"])
    memory.add_evidence(
        turn=3,
        handle="C002",
        operation="ENTITY_CONTEXT",
        category="entity",
        summary={"identity": "C002"},
    )
    assert E9InvestigationRuntime._submit_ready(memory, ["C002"])


def test_final_interface_initial_surface_has_no_redundant_observation_calls() -> None:
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    surface = control_surface(
        memory.state, turn=1, max_steps=12, max_rejections=2, semantic_limit=24
    )
    assert surface.actions == ("HYPOTHESIZE", "STOP")
    assert surface.operations == ()
    assert surface.capabilities() == {
        "HYPOTHESIZE": {"targets": ("C001",), "operations": ()},
        "STOP": {"targets": (), "operations": ()},
    }


def test_event_analysis_is_candidate_scoped_and_metric_capability_is_observable(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, [])
    incident, _ = build_observable_incident(runtime.backend)
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(({"canonical": "otel-demo/Service/frontend"},))
    memory.append("HYPOTHESIS_PROPOSED", 1, {"entity_handle": "C001", "rationale": "test"})
    resolver = SemanticCapabilityResolver(runtime.backend, memory, incident)
    available = resolver.available_operations(phase="VERIFY", target_handle="C001")
    assert "EVENT_ANALYSIS" in available
    assert "METRIC_ANOMALIES" not in available
    assert (
        next(spec for spec in resolver_specs() if spec.name == "EVENT_ANALYSIS").scope == "TARGET"
    )


def resolver_specs() -> tuple[Any, ...]:
    from packages.evals.itbench.e9_semantic import E9_SEMANTIC_OPERATION_SPECS

    return E9_SEMANTIC_OPERATION_SPECS


def test_action_specific_schema_rejects_cross_product_combinations(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, [])
    incident, alerts = build_observable_incident(runtime.backend)
    memory = E9CaseMemory(execution_id="ITB-E9", scenario_id="Scenario-1")
    memory.discover_entities(
        ({"canonical": "otel-demo/Service/frontend"}, {"canonical": "otel-demo/Service/other"})
    )
    memory.append("HYPOTHESIS_PROPOSED", 1, {"entity_handle": "C001", "rationale": "test"})
    request, _ = runtime.build_request(
        incident, alerts, memory, run_id=incident.incident_id, turn=2
    )
    schema = next(
        item["parameters"]
        for item in OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)["tools"]
        if item["name"] == "request_itbench_tools"
    )
    assert (
        _json_schema_error(
            {
                "action": "HYPOTHESIZE",
                "target": "C001",
                "operation": "ENTITY_CONTEXT",
                "rationale": None,
            },
            schema,
        )
        is not None
    )
    assert (
        _json_schema_error(
            {
                "action": "INVESTIGATE",
                "target": "C002",
                "operation": "ENTITY_CONTEXT",
                "rationale": None,
            },
            schema,
        )
        is not None
    )
    assert (
        _json_schema_error({"action": "REVISE", "target": "C001", "rationale": None}, schema)
        is not None
    )
    assert (
        _json_schema_error(
            {
                "action": "INVESTIGATE",
                "target": "C001",
                "operation": "ENTITY_CONTEXT",
                "rationale": None,
            },
            schema,
        )
        is None
    )
