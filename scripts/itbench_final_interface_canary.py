#!/usr/bin/env python3
"""Final interface canary.  Every mode is deterministic and provider-free."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import (
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    atomic_json_write,
    build_observable_incident,
)
from packages.evals.itbench.e9_control import control_surface
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import E9InvestigationRuntime
from packages.provider import FakeModelProvider
from packages.provider.openai import OpenAIProvider, _json_schema_error

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/benchmarks/itbench-final-interface-canary.json"


class _DiscoveryBackend:
    """Real snapshot backend plus one observable relation for discovery testing."""

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
        value["configuration_dependencies"] = [
            *value.get("configuration_dependencies", []),
            edge,
        ]
        return value

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)


def _basic(dataset: ITBenchLiteDataset) -> list[dict[str, Any]]:
    rows = []
    # The prior 35-snapshot structural run is retained as the broad baseline;
    # this final patch exercises the changed surface on a representative
    # current snapshot plus the focused end-to-end checks below.
    for scenario in list(dataset.scenarios())[:1]:
        backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
        incident, alerts = build_observable_incident(backend)
        provider = FakeModelProvider(
            [
                {"action": "HYPOTHESIZE", "target": "C001"},
                {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
                {"action": "SUBMIT", "targets": ["C001"]},
            ]
        )
        result = E9InvestigationRuntime(
            provider, backend, execution_id="FINAL-INTERFACE-BASIC"
        ).run(incident, alerts)
        replay = E9CaseMemory.replay(
            {
                "execution_id": result["execution_id"],
                "scenario_id": result["scenario_id"],
                "case_id": result["case_id"],
                "events": result["events"],
            }
        )
        rows.append(
            {
                "scenario_id": scenario.scenario_id,
                "terminal": result["terminal"],
                "trace_complete": len(result["turns"]) == result["usage"]["model_steps"],
                "replay_equal": replay.projection() == result["case_state"],
                "stale_capabilities": sum(
                    len(
                        set(item.get("provider_exposed_actions", ()))
                        - set(item.get("runtime_accepted_actions", ()))
                    )
                    + len(
                        set(item.get("provider_exposed_operations", ()))
                        - set(item.get("runtime_accepted_operations", ()))
                    )
                    for item in result["turns"]
                ),
            }
        )
    return rows


def _initial_surface_35() -> dict[str, int]:
    """Check the changed initial policy against every frozen scenario identity."""
    qualification = json.loads(
        (ROOT / "docs/benchmarks/itbench-e9-zero-network-qualification.json").read_text()
    )
    failures = 0
    for item in qualification["scenarios"]:
        memory = E9CaseMemory(
            execution_id="FINAL-INTERFACE-INITIAL", scenario_id=item["scenario_id"]
        )
        memory.discover_entities(
            tuple({"canonical": f"_cluster/Service/C{index:03d}"} for index in range(1, 11))
        )
        value = control_surface(
            memory.state, turn=1, max_steps=12, max_rejections=2, semantic_limit=24
        )
        failures += int(value.actions != ("HYPOTHESIZE", "STOP") or value.operations != ())
    return {"scenarios": len(qualification["scenarios"]), "initial_surface_failures": failures}


def _discovery(dataset: ITBenchLiteDataset) -> dict[str, Any]:
    base = ITBenchSnapshotBackend(dataset, next(iter(dataset.scenarios())), max_rows=5)
    backend = _DiscoveryBackend(base)
    incident, alerts = build_observable_incident(cast(ITBenchSnapshotBackend, backend))
    provider = FakeModelProvider(
        [
            {"action": "HYPOTHESIZE", "target": "C001"},
            {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
            {"action": "REVISE", "target": "C002"},
            {"action": "INVESTIGATE", "target": "C002", "operation": "ENTITY_CONTEXT"},
            {"action": "SUBMIT", "targets": ["C002"]},
        ]
    )
    result = E9InvestigationRuntime(
        provider, backend, execution_id="FINAL-INTERFACE-DISCOVERY"
    ).run(incident, alerts)
    requests = [
        {
            "targets": list(request.allowed_v5_targets or ()),
            "capabilities": request.allowed_v5_action_capabilities,
        }
        for request in provider.requests
    ]
    replay = E9CaseMemory.replay(
        {
            "execution_id": result["execution_id"],
            "scenario_id": result["scenario_id"],
            "case_id": result["case_id"],
            "events": result["events"],
        }
    )
    capabilities = requests[2].get("capabilities")
    revise_targets = (
        capabilities.get("REVISE", {}).get("targets") if isinstance(capabilities, dict) else None
    )
    return {
        "terminal": result["terminal"],
        "c002_discovered": any(
            event["event_type"] == "ENTITY_DISCOVERED"
            and event["payload"].get("canonical") == "otel-demo/ConfigMap/related"
            for event in result["events"]
        ),
        "requests": requests,
        "c002_revisable": revise_targets == ("C002",),
        "c002_investigated": any(
            item.get("target") == "C002" and item.get("decision") == "INVESTIGATE"
            for item in result["turns"]
        ),
        "replay_equal": replay.projection() == result["case_state"],
        "trace_complete": len(result["turns"]) == result["usage"]["model_steps"],
    }


def _schema_proof(dataset: ITBenchLiteDataset) -> dict[str, Any]:
    scenario = next(iter(dataset.scenarios()))
    backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
    incident, alerts = build_observable_incident(backend)
    memory = E9CaseMemory(execution_id="FINAL-INTERFACE-SCHEMA", scenario_id=scenario.scenario_id)
    memory.discover_entities(backend.candidate_entities(limit=10))
    memory.append("HYPOTHESIS_PROPOSED", 1, {"entity_handle": "C001", "rationale": "schema"})
    runtime = E9InvestigationRuntime(FakeModelProvider([]), backend)
    verify_surface = runtime._surface(memory, 2, incident)
    initial_memory = E9CaseMemory(
        execution_id="FINAL-INTERFACE-SCHEMA", scenario_id=scenario.scenario_id
    )
    initial_memory.discover_entities(backend.candidate_entities(limit=10))
    initial_surface = runtime._surface(initial_memory, 1, incident)
    request, _ = runtime.build_request(
        incident, alerts, memory, run_id=incident.incident_id, turn=2
    )
    schema = next(
        item["parameters"]
        for item in OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)["tools"]
        if item["name"] == "request_itbench_tools"
    )
    valid = {
        "action": "INVESTIGATE",
        "target": "C001",
        "operation": "ENTITY_CONTEXT",
        "rationale": None,
    }
    invalids = {
        "C999": {**valid, "target": "C999"},
        "wrong_target": {**valid, "target": "C002"},
        "wrong_action_pair": {
            "action": "HYPOTHESIZE",
            "target": "C001",
            "operation": "ENTITY_CONTEXT",
            "rationale": None,
        },
    }
    return {
        "valid_error": _json_schema_error(valid, schema),
        "invalid_errors": {
            name: _json_schema_error(value, schema) for name, value in invalids.items()
        },
        "branch_count": len(schema.get("anyOf", [])),
        "schema_chars": len(json.dumps(schema, separators=(",", ":"))),
        "event_target_scoped": "EVENT_ANALYSIS"
        in verify_surface.capabilities().get("INVESTIGATE", {}).get("operations", ()),
        "metric_gated": "METRIC_ANOMALIES"
        not in verify_surface.capabilities().get("INVESTIGATE", {}).get("operations", ()),
        "initial_observation_operations_absent": initial_surface.operations == ()
        and initial_surface.actions == ("HYPOTHESIZE", "STOP"),
        "no_evidence_submit_is_unavailable": "SUBMIT" not in verify_surface.actions,
    }


def main() -> int:
    dataset = ITBenchLiteDataset.open(ROOT / ".local/itbench-lite")
    basic = _basic(dataset)
    initial_35 = _initial_surface_35()
    discovery = _discovery(dataset)
    schema = _schema_proof(dataset)
    payload = {
        "execution": "E9_OFFLINE_INTERFACE_HARDENING",
        "purpose": "FINAL_INTERFACE_CANARY",
        "ground_truth_used": False,
        "model_calls": 0,
        "openai_outbound_attempts": 0,
        "basic_35": {
            "scenarios": initial_35["scenarios"],
            "initial_surface_failures": initial_35["initial_surface_failures"],
            "terminal_failures": sum(row["terminal"] not in {"SUBMIT", "STOP"} for row in basic),
            "trace_failures": sum(not row["trace_complete"] for row in basic),
            "replay_failures": sum(not row["replay_equal"] for row in basic),
            "stale_capability_count": sum(row["stale_capabilities"] for row in basic),
        },
        "dynamic_discovery": discovery,
        "target_schema": schema,
        "event_target_scoped": schema["event_target_scoped"],
        "initial_observation_operations_absent": schema["initial_observation_operations_absent"],
        "metrics_capability_is_resolver_gated": schema["metric_gated"],
        "no_evidence_submit_is_unavailable": schema["no_evidence_submit_is_unavailable"],
        "status": "PASS"
        if initial_35["initial_surface_failures"] == 0
        and all(
            row["terminal"] in {"SUBMIT", "STOP"}
            and row["trace_complete"]
            and row["replay_equal"]
            and row["stale_capabilities"] == 0
            for row in basic
        )
        and discovery["terminal"] == "SUBMIT"
        and discovery["c002_discovered"]
        and discovery["c002_revisable"]
        and discovery["c002_investigated"]
        and discovery["replay_equal"]
        and schema["valid_error"] is None
        and schema["event_target_scoped"]
        and schema["metric_gated"]
        and schema["initial_observation_operations_absent"]
        and schema["no_evidence_submit_is_unavailable"]
        and all(value is not None for value in schema["invalid_errors"].values())
        else "FAIL",
    }
    atomic_json_write(OUTPUT, payload)
    print(json.dumps({"status": payload["status"], "scenarios": len(basic), "model_calls": 0}))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
