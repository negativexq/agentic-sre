#!/usr/bin/env python3
"""Materialize the final offline model-facing decision surface; no provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    build_observable_incident,
)
from packages.evals.itbench.e9_control import control_surface
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_semantic import SemanticCapabilityResolver

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-live-smoke-capability-surface-v2.json"
MARKDOWN = ROOT / "docs/benchmarks/itbench-live-smoke-capability-surface-v2.md"


def surface(
    memory: E9CaseMemory, backend: ITBenchSnapshotBackend, incident: Any, turn: int
) -> dict[str, Any]:
    target = memory.state.get("current_hypothesis") or {}
    handle = target.get("entity_handle") if isinstance(target, dict) else None
    available = SemanticCapabilityResolver(backend, memory, incident).available_operations(
        phase=memory.state["current_phase"], target_handle=handle
    )
    value = control_surface(
        memory.state,
        turn=turn,
        max_steps=12,
        max_rejections=2,
        semantic_limit=24,
        available_operations=available,
    )
    return {
        "phase": value.phase,
        "branches": {
            action: {"targets": list(targets), "operations": list(operations)}
            for action, targets, operations in value.action_capabilities
        },
    }


def main() -> int:
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    scenarios = list(dataset.scenarios())
    sample: dict[str, Any] = {}
    coverage = json.loads(
        (ROOT / "docs/benchmarks/itbench-semantic-operation-coverage-v3.json").read_text()
    )
    capability_rows = {row["operation"]: row for row in coverage["operations"]}
    metrics_case = capability_rows["METRIC_ANOMALIES"].get("available", 0)
    trace_case = capability_rows["TRACE_ERROR_TREE"].get("available", 0)
    scenario = scenarios[0]
    backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
    incident, _ = build_observable_incident(backend)
    memory = E9CaseMemory(execution_id="E9-SURFACE-V2", scenario_id=scenario.scenario_id)
    memory.discover_entities(backend.candidate_entities(limit=10))
    sample["INITIAL"] = surface(memory, backend, incident, 1)
    memory.append("HYPOTHESIS_PROPOSED", 1, {"entity_handle": "C001", "rationale": "surface test"})
    sample["VERIFY_STANDARD"] = surface(memory, backend, incident, 2)
    sample["VERIFY_METRICS_CAPABLE_COUNT"] = metrics_case
    sample["VERIFY_TRACE_CAPABLE_COUNT"] = trace_case
    payload = {
        "execution": "E9_OFFLINE_INTERFACE_HARDENING",
        "ground_truth_used": False,
        "model_calls": 0,
        "openai_outbound_attempts": 0,
        "sample_surfaces": sample,
        "metrics_capable_scenarios": metrics_case,
        "trace_capable_scenarios": trace_case,
        "disabled_by_default": [
            "INCIDENT_OVERVIEW",
            "ALERT_ANALYSIS",
            "TOPOLOGY_ANALYSIS",
            "RECENT_CHANGE_ANALYSIS",
            "COMPARE_REPLICAS",
            "VERIFY_TEMPORAL_ALIGNMENT",
        ],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    MARKDOWN.write_text(
        "# ITBench live-smoke capability surface v2 (offline)\n\n"
        "The exact generated branch surfaces are in the JSON artifact.\n\n"
        "## INITIAL\n\n"
        "`HYPOTHESIZE(target=C001…C010)` or `STOP`; semantic operation selection is empty.\n\n"
        "## VERIFY\n\n"
        "`INVESTIGATE(target=current_hypothesis, operation=ENTITY_CONTEXT|EVENT_ANALYSIS|SPEC_ANALYSIS)`; `REVISE(target=visible alternative)`; `STOP`. `SUBMIT` appears only once candidate-associated evidence exists. Metric and trace operations are added only for the measured capable snapshot.\n\n"
        "Default-disabled rediscovery/dead capabilities: `INCIDENT_OVERVIEW`, `ALERT_ANALYSIS`, `TOPOLOGY_ANALYSIS`, `RECENT_CHANGE_ANALYSIS`, `COMPARE_REPLICAS`, `VERIFY_TEMPORAL_ALIGNMENT`. No provider was constructed.\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "PASS", "scenarios": len(scenarios), "model_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
