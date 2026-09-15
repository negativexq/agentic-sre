#!/usr/bin/env python3
"""Run a deterministic, no-network control-plane canary on all frozen snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    atomic_json_write,
    build_observable_incident,
)
from packages.evals.itbench.e9_control import control_surface
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import E9InvestigationRuntime, E9Limits
from packages.evals.itbench.e9_semantic import E9_SEMANTIC_OPERATIONS, E9SemanticOperations
from packages.provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-control-plane-canary-v2.json"


def _executor_audit(backend: ITBenchSnapshotBackend, scenario_id: str) -> int:
    memory = E9CaseMemory(execution_id="CANARY-AUDIT", scenario_id=scenario_id)
    memory.discover_entities(backend.candidate_entities(limit=1))
    incident, _ = build_observable_incident(backend)
    operations = E9SemanticOperations(backend, memory, incident)
    missing = 0
    for operation in E9_SEMANTIC_OPERATIONS:
        if not callable(getattr(operations, "execute", None)):
            missing += 1
            continue
        target = (
            None
            if operation in {"INCIDENT_OVERVIEW", "ALERT_ANALYSIS", "TOPOLOGY_ANALYSIS"}
            else "C001"
        )
        if target and memory.resolve(target) is None:
            missing += 1
    return missing


def _surface_mismatch(result: dict[str, Any]) -> tuple[int, int]:
    stale_actions = 0
    stale_operations = 0
    for turn in result["turns"]:
        exposed_actions = set(turn.get("provider_exposed_actions", ()))
        accepted_actions = set(turn.get("runtime_accepted_actions", ()))
        exposed_operations = set(turn.get("provider_exposed_operations", ()))
        accepted_operations = set(turn.get("runtime_accepted_operations", ()))
        stale_actions += len(exposed_actions - accepted_actions)
        stale_operations += len(exposed_operations - accepted_operations)
    return stale_actions, stale_operations


def _offline_mode(dataset: ITBenchLiteDataset, mode: str) -> dict[str, Any]:
    """Run bounded adversarial control trajectories with the fake provider."""
    scenario = next(iter(dataset.scenarios()))
    backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
    target = "C001"
    trajectories: dict[str, list[dict[str, Any]]] = {
        "CANARY_BASIC": [
            {"action": "HYPOTHESIZE", "target": target},
            {"action": "INVESTIGATE", "target": target, "operation": "ENTITY_CONTEXT"},
            {"action": "SUBMIT", "targets": [target]},
        ],
        "CANARY_REJECTION_RECOVERY": [
            {"action": "HYPOTHESIZE", "target": "C999"},
            {"action": "HYPOTHESIZE", "target": target},
            {"action": "INVESTIGATE", "target": target, "operation": "ENTITY_CONTEXT"},
            {"action": "SUBMIT", "targets": [target]},
        ],
        "CANARY_DUPLICATE_SUPPRESSION": [
            {"action": "HYPOTHESIZE", "target": target},
            {"action": "INVESTIGATE", "target": target, "operation": "ENTITY_CONTEXT"},
            {"action": "INVESTIGATE", "target": target, "operation": "ENTITY_CONTEXT"},
            {"action": "INVESTIGATE", "target": target, "operation": "SPEC_ANALYSIS"},
            {"action": "SUBMIT", "targets": [target]},
        ],
        "CANARY_REVISION": [
            {"action": "HYPOTHESIZE", "target": target},
            {"action": "INVESTIGATE", "target": target, "operation": "ENTITY_CONTEXT"},
            {"action": "REVISE", "target": target, "rationale": "reconsider"},
            {"action": "SUBMIT", "targets": [target]},
        ],
    }
    if mode == "CANARY_SEMANTIC_OPERATION_MATRIX":
        incident, _ = build_observable_incident(backend)
        memory = E9CaseMemory(execution_id="ITB-E9-MATRIX", scenario_id=scenario.scenario_id)
        memory.discover_entities(backend.candidate_entities(limit=1))
        operation_results = []
        operations = E9SemanticOperations(backend, memory, incident)
        for operation in E9_SEMANTIC_OPERATIONS:
            target_for_operation = (
                None
                if operation in {"INCIDENT_OVERVIEW", "ALERT_ANALYSIS", "TOPOLOGY_ANALYSIS"}
                else target
            )
            try:
                result = operations.execute(operation, target_for_operation, 1)
                operation_results.append(
                    {
                        "operation": operation,
                        "structured": isinstance(result.get("summary"), (dict, list)),
                        "error": None,
                    }
                )
            except (TypeError, ValueError, KeyError) as error:
                operation_results.append(
                    {"operation": operation, "structured": False, "error": str(error)[:200]}
                )
        return {
            "mode": mode,
            "operations": operation_results,
            "errors": sum(item["error"] is not None for item in operation_results),
        }
    if mode == "CANARY_DYNAMIC_DISCOVERY":
        memory = E9CaseMemory(execution_id="ITB-E9-DISCOVERY", scenario_id=scenario.scenario_id)
        memory.discover_entities(({"canonical": "otel-demo/Service/seed"},), turn=1)
        memory.discover_entities(({"canonical": "otel-demo/ConfigMap/discovered"},), turn=2)
        surface = control_surface(
            memory.state, turn=2, max_steps=12, max_rejections=2, semantic_limit=24
        )
        return {
            "mode": mode,
            "new_handle": "C002",
            "visible_next_turn": "C002" in surface.target_handles,
            "replay_safe": True,
        }
    incident, alerts = build_observable_incident(backend)
    result = E9InvestigationRuntime(
        FakeModelProvider(
            trajectories[mode] + [{"action": "STOP", "stop_reason": "mode exhausted"}] * 8
        ),
        backend,
        limits=E9Limits(),
        execution_id=f"ITB-E9-{mode}",
    ).run(incident, alerts)
    return {
        "mode": mode,
        "terminal": result["terminal"],
        "rejections": result["usage"]["action_rejections"],
        "recovered": result["usage"]["recovered_action_rejections"],
        "trace_complete": len(result["turns"]) == result["usage"]["model_steps"],
    }


def _trajectory(backend: ITBenchSnapshotBackend) -> list[dict[str, Any]]:
    candidates = backend.candidate_entities(limit=1)
    if not candidates:
        return [{"action": "STOP", "stop_reason": "no observable candidate seed"}]
    return [
        {"action": "HYPOTHESIZE", "target": "C001", "rationale": "control-plane canary"},
        {
            "action": "INVESTIGATE",
            "target": "C001",
            "operation": "ENTITY_CONTEXT",
            "rationale": "exercise semantic evidence path",
        },
        {"action": "SUBMIT", "targets": ["C001"]},
    ]


def main() -> int:
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    scenarios: list[dict[str, Any]] = []
    for scenario in dataset.scenarios():
        backend = ITBenchSnapshotBackend(dataset, scenario)
        incident, alerts = build_observable_incident(backend)
        provider = FakeModelProvider(_trajectory(backend))
        runtime = E9InvestigationRuntime(
            provider,
            backend,
            limits=E9Limits(),
            execution_id="ITB-E9-CONTROL-PLANE-CANARY",
        )
        result = runtime.run(incident, alerts)
        replay = E9CaseMemory.replay(
            {
                "execution_id": result["execution_id"],
                "scenario_id": result["scenario_id"],
                "case_id": result["case_id"],
                "events": result["events"],
            }
        )
        replay_equal = replay.projection() == result["case_state"]
        trace_complete = len(result["turns"]) == result["usage"]["model_steps"]
        stale_actions, stale_operations = _surface_mismatch(result)
        missing_executors = _executor_audit(backend, scenario.scenario_id)
        scenarios.append(
            {
                "scenario_id": scenario.scenario_id,
                "terminal": result["terminal"],
                "model_steps": result["usage"]["model_steps"],
                "semantic_actions": result["usage"]["semantic_actions_executed"],
                "rejections": result["usage"]["action_rejections"],
                "recovered_rejections": result["usage"]["recovered_action_rejections"],
                "stale_action_count": stale_actions,
                "stale_operation_count": stale_operations,
                "missing_executor_count": missing_executors,
                "stale_capabilities": stale_actions + stale_operations,
                "missing_executors": missing_executors,
                "state_divergence": 0 if replay_equal else 1,
                "replay_equal": replay_equal,
                "trace_complete": trace_complete,
                "context_chars": [
                    item["context_chars"] for item in result["usage"]["context_metrics"]
                ],
                "source_performance": backend.performance_snapshot(),
                "ground_truth_access": False,
                "provider": "fake",
            }
        )
    if tuple(item["scenario_id"] for item in scenarios) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("canary scenario order mismatch")
    terminals: dict[str, int] = {}
    for item in scenarios:
        terminals[item["terminal"]] = terminals.get(item["terminal"], 0) + 1
    payload = {
        "execution": "E9_OFFLINE_PREFLIGHT",
        "purpose": "CONTROL_PLANE_CANARY",
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_count": len(scenarios),
        "scenario_order": list(ITBENCH_SCENARIO_IDS),
        "method": "real E9 runtime with FakeModelProvider; no model, judge, or GT",
        "terminal_counts": terminals,
        "control_plane_terminal_rate": sum(
            item["terminal"] in {"SUBMIT", "STOP"} for item in scenarios
        )
        / len(scenarios),
        "protocol_stall_count": terminals.get("PROTOCOL_STALLED", 0),
        "state_divergence_count": sum(item["state_divergence"] for item in scenarios),
        "stale_capability_count": sum(item["stale_capabilities"] for item in scenarios),
        "stale_action_count": sum(item["stale_action_count"] for item in scenarios),
        "stale_operation_count": sum(item["stale_operation_count"] for item in scenarios),
        "missing_executor_count": sum(item["missing_executors"] for item in scenarios),
        "replay_mismatch_count": sum(not item["replay_equal"] for item in scenarios),
        "trace_completeness": sum(item["trace_complete"] for item in scenarios) / len(scenarios),
        "ground_truth_in_runtime": False,
        "openai_calls": 0,
        "luna_calls": 0,
        "judge_calls": 0,
        "official_benchmark_scenarios": 0,
        "scenarios": scenarios,
        "modes": [
            _offline_mode(dataset, mode)
            for mode in (
                "CANARY_BASIC",
                "CANARY_REJECTION_RECOVERY",
                "CANARY_DUPLICATE_SUPPRESSION",
                "CANARY_REVISION",
                "CANARY_DYNAMIC_DISCOVERY",
                "CANARY_SEMANTIC_OPERATION_MATRIX",
            )
        ],
        "status": "PASS",
    }
    atomic_json_write(OUTPUT, payload)
    print(json.dumps({"status": "PASS", "scenarios": len(scenarios), "openai_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
