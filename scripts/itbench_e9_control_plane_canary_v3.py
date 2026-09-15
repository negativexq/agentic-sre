#!/usr/bin/env python3
"""Offline control-plane canaries with measured capability and target surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    atomic_json_write,
    build_observable_incident,
)
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import E9InvestigationRuntime, E9Limits
from packages.evals.itbench.e9_semantic import (
    E9_SEMANTIC_OPERATIONS,
    E9SemanticOperations,
)
from packages.provider import FakeModelProvider
from packages.provider.openai import OpenAIProvider, _json_schema_error

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-control-plane-canary-v3.json"


def _surface_snapshot(result: dict[str, Any]) -> dict[str, int]:
    stale_actions = stale_operations = stale_targets = 0
    for turn in result["turns"]:
        stale_actions += len(
            set(turn.get("provider_exposed_actions", ()))
            - set(turn.get("runtime_accepted_actions", ()))
        )
        stale_operations += len(
            set(turn.get("provider_exposed_operations", ()))
            - set(turn.get("runtime_accepted_operations", ()))
        )
        exposed = set(turn.get("provider_exposed_targets", ()))
        accepted = set(turn.get("runtime_accepted_targets", ()))
        stale_targets += len(exposed - accepted)
    return {
        "stale_action_count": stale_actions,
        "stale_operation_count": stale_operations,
        "stale_target_count": stale_targets,
    }


def _dispatch_matrix(backend: ITBenchSnapshotBackend, scenario_id: str) -> dict[str, Any]:
    incident, _ = build_observable_incident(backend)
    rows = []
    for operation in E9_SEMANTIC_OPERATIONS:
        memory = E9CaseMemory(execution_id="CANARY-V3-MATRIX", scenario_id=scenario_id)
        candidates = backend.candidate_entities(limit=1)
        if candidates:
            memory.discover_entities(candidates)
        target = (
            "C001"
            if operation
            in {
                "ENTITY_CONTEXT",
                "METRIC_ANOMALIES",
                "TRACE_ERROR_TREE",
                "SPEC_ANALYSIS",
                "COMPARE_REPLICAS",
                "VERIFY_TEMPORAL_ALIGNMENT",
            }
            else None
        )
        try:
            value = E9SemanticOperations(backend, memory, incident).execute(operation, target, 1)
            summary = value["summary"]
            rows.append(
                {
                    "operation": operation,
                    "dispatch": "PASS",
                    "structured": isinstance(summary, (dict, list)),
                    "chars": len(json.dumps(summary, default=str)),
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            rows.append(
                {
                    "operation": operation,
                    "dispatch": "ERROR",
                    "structured": False,
                    "chars": 0,
                    "error": str(error)[:200],
                }
            )
    return {
        "rows": rows,
        "semantic_dispatch_errors": sum(row["dispatch"] == "ERROR" for row in rows),
    }


class _DiscoveryBackend:
    """Delegate to a real snapshot backend while adding one observable topology edge."""

    def __init__(self, base: ITBenchSnapshotBackend) -> None:
        self.base = base
        self.scenario = base.scenario
        self.max_rows = base.max_rows

    def candidate_entities(self, limit: int = 10) -> tuple[dict[str, Any], ...]:
        return ({"canonical": "otel-demo/Service/seed"},)[:limit]

    def query_entity_context(
        self, entity: str, limit: int, *, include_telemetry: bool = True
    ) -> dict[str, Any]:
        value = self.base.query_entity_context(entity, limit, include_telemetry=include_telemetry)
        edge = {
            "source": "otel-demo/Service/seed",
            "target": "otel-demo/ConfigMap/related",
            "relationship": "configuration_reference",
        }
        value["topology"] = [*value.get("topology", []), edge]
        value["configuration_dependencies"] = [*value.get("configuration_dependencies", []), edge]
        return value

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)


def _dynamic_discovery(dataset: ITBenchLiteDataset) -> dict[str, Any]:
    base = ITBenchSnapshotBackend(dataset, next(iter(dataset.scenarios())), max_rows=5)
    backend = _DiscoveryBackend(base)
    incident, alerts = build_observable_incident(cast(ITBenchSnapshotBackend, backend))
    provider = FakeModelProvider(
        [
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": "seed"},
            {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
            {"action": "REVISE", "target": "C002", "rationale": "follow dependency"},
            {"action": "INVESTIGATE", "target": "C002", "operation": "ENTITY_CONTEXT"},
            {"action": "SUBMIT", "targets": ["C002"]},
        ]
    )
    result = E9InvestigationRuntime(
        provider, backend, limits=E9Limits(), execution_id="CANARY-V3-DISCOVERY"
    ).run(incident, alerts)
    requests = []
    for request in provider.requests:
        requests.append(
            {
                "targets": list(request.allowed_v5_targets or ()),
                "actions": list(request.allowed_v5_actions or ()),
                "operations": list(request.allowed_v5_operations or ()),
            }
        )
    events = [
        event
        for event in result["events"]
        if event["event_type"] in {"ENTITY_DISCOVERED", "HYPOTHESIS_REVISED", "DIAGNOSIS_SUBMITTED"}
    ]
    replay = E9CaseMemory.replay(
        {
            "execution_id": result["execution_id"],
            "scenario_id": result["scenario_id"],
            "case_id": result["case_id"],
            "events": result["events"],
        }
    )
    schema_parameters = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(
        provider.requests[2]
    )
    schema = next(
        item["parameters"]
        for item in schema_parameters["tools"]
        if item["name"] == "request_itbench_tools"
    )
    valid_payload: dict[str, object] = {
        "action": "REVISE",
        "target": "C002",
        "targets": [],
        "operation": None,
        "rationale": None,
    }
    return {
        "terminal": result["terminal"],
        "turns": len(result["turns"]),
        "requests": requests,
        "events": events,
        "c002_visible": any("C002" in item["targets"] for item in requests[2:]),
        "c002_revisable": any(
            item.get("decision") == "REVISE" and item.get("target") == "C002"
            for item in result["turns"]
        ),
        "c002_evidence": bool(result["case_state"].get("evidence_by_candidate", {}).get("C002")),
        "replay_equal": replay.projection() == result["case_state"],
        "direct_discovery_injected": False,
        "target_schema": {
            "valid_targets": list(provider.requests[2].allowed_v5_targets or ()),
            "valid_c002_error": _json_schema_error(valid_payload, schema),
            "invalid_c999_error": _json_schema_error({**valid_payload, "target": "C999"}, schema),
        },
    }


def _capability_gate_proof(dataset: ITBenchLiteDataset) -> dict[str, Any]:
    scenario = next(iter(dataset.scenarios()))
    backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
    incident, alerts = build_observable_incident(backend)
    memory = E9CaseMemory(execution_id="CANARY-V3-CAPABILITY", scenario_id=scenario.scenario_id)
    memory.discover_entities(backend.candidate_entities(limit=1))
    memory.append(
        "HYPOTHESIS_PROPOSED", 1, {"entity_handle": "C001", "rationale": "capability proof"}
    )
    runtime = E9InvestigationRuntime(FakeModelProvider([]), backend)
    request, _ = runtime.build_request(
        incident, alerts, memory, run_id=incident.incident_id, turn=2
    )
    context, _ = runtime.planner.plan(
        backend, incident, alerts, memory, None, turn=2, max_steps=12, semantic_limit=24
    )
    workflow = json.loads(context)["workflow"]
    accepted = runtime._surface(memory, 2, incident)
    return {
        "operation": "TRACE_ERROR_TREE",
        "provider_exposed": "TRACE_ERROR_TREE" in (request.allowed_v5_operations or ()),
        "context_exposed": "TRACE_ERROR_TREE" in workflow["valid_operations"],
        "runtime_exposed": "TRACE_ERROR_TREE" in accepted.operations,
    }


def main() -> int:
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    scenarios = []
    for index, scenario in enumerate(dataset.scenarios()):
        backend = ITBenchSnapshotBackend(dataset, scenario)
        incident, alerts = build_observable_incident(backend)
        provider = FakeModelProvider(
            [
                {"action": "HYPOTHESIZE", "target": "C001"},
                {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
                {"action": "SUBMIT", "targets": ["C001"]},
            ]
        )
        result = E9InvestigationRuntime(
            provider, backend, limits=E9Limits(), execution_id="CANARY-V3-BASIC"
        ).run(incident, alerts)
        replay = E9CaseMemory.replay(
            {
                "execution_id": result["execution_id"],
                "scenario_id": result["scenario_id"],
                "case_id": result["case_id"],
                "events": result["events"],
            }
        )
        surfaces = _surface_snapshot(result)
        dispatch = (
            _dispatch_matrix(backend, scenario.scenario_id)
            if index == 0
            else {
                "semantic_dispatch_errors": None,
                "rows": [],
                "dispatch_scope": "representative scenario only",
            }
        )
        scenarios.append(
            {
                "scenario_id": scenario.scenario_id,
                "terminal": result["terminal"],
                "model_steps": result["usage"]["model_steps"],
                "replay_equal": replay.projection() == result["case_state"],
                "trace_complete": len(result["turns"]) == result["usage"]["model_steps"],
                **surfaces,
                **dispatch,
            }
        )
    dynamic = _dynamic_discovery(dataset)
    payload = {
        "execution": "E9_OFFLINE_PREFLIGHT",
        "purpose": "CONTROL_PLANE_CANARY_V3",
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_order": list(ITBENCH_SCENARIO_IDS),
        "ground_truth_used": False,
        "openai_calls": 0,
        "judge_calls": 0,
        "basic_35_snapshot": scenarios,
        "basic_terminal_counts": {
            terminal: sum(item["terminal"] == terminal for item in scenarios)
            for terminal in {item["terminal"] for item in scenarios}
        },
        "stale_capability_count": sum(
            item["stale_action_count"] + item["stale_operation_count"] + item["stale_target_count"]
            for item in scenarios
        ),
        "missing_executor_count": sum(
            int(item["semantic_dispatch_errors"])
            for item in scenarios
            if item["semantic_dispatch_errors"] is not None
        ),
        "semantic_dispatch_scope": "all operations on one representative snapshot; basic structural path on all 35",
        "replay_mismatch_count": sum(not item["replay_equal"] for item in scenarios),
        "trace_completeness": sum(item["trace_complete"] for item in scenarios) / len(scenarios),
        "modes": {
            "CANARY_DYNAMIC_DISCOVERY": dynamic,
            "CANARY_TARGET_SCHEMA": dynamic["target_schema"],
            "CANARY_CAPABILITY_GATING": _capability_gate_proof(dataset),
        },
        "status": "PASS"
        if not any(
            item["stale_action_count"]
            + item["stale_operation_count"]
            + item["stale_target_count"]
            + (item["semantic_dispatch_errors"] or 0)
            + (not item["replay_equal"])
            for item in scenarios
        )
        and dynamic["terminal"] in {"SUBMIT", "STOP"}
        and dynamic["replay_equal"]
        else "FAIL",
    }
    atomic_json_write(OUTPUT, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "scenarios": len(scenarios),
                "dynamic_terminal": dynamic["terminal"],
                "openai_calls": 0,
            }
        )
    )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
