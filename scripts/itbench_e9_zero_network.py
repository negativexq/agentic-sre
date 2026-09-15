#!/usr/bin/env python3
"""Qualify E9 snapshot projections without model, judge, or ground truth access."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    build_observable_incident,
)
from packages.evals.itbench.e9_context import E9ContextPlanner
from packages.evals.itbench.e9_fsm import E9FSM
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import E9Limits
from packages.evals.itbench.persistence import atomic_json_write

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-e9-zero-network-qualification.json"


def main() -> int:
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    scenarios: list[dict[str, Any]] = []
    planner = E9ContextPlanner()
    for scenario in dataset.scenarios():
        backend = ITBenchSnapshotBackend(dataset, scenario)
        incident, alerts = build_observable_incident(backend)
        memory = E9CaseMemory(execution_id="ITB-E9", scenario_id=scenario.scenario_id)
        memory.discover_entities(backend.candidate_entities(limit=10))
        fsm = E9FSM()
        context, sections = planner.plan(
            backend,
            incident,
            alerts,
            memory,
            fsm,
            turn=1,
            max_steps=E9Limits().max_model_calls,
            semantic_limit=E9Limits().max_tool_calls,
        )
        lowered = context.casefold()
        if "ground_truth" in lowered or len(context) > 25_000:
            raise RuntimeError(f"unsafe or unbounded E9 context: {scenario.scenario_id}")
        replay_payload = {
            "execution_id": memory.execution_id,
            "scenario_id": memory.scenario_id,
            "case_id": memory.case_id,
            "events": [event.as_dict() for event in memory.events],
        }
        replayed = E9CaseMemory.replay(replay_payload)
        if replayed.projection() != memory.projection():
            raise RuntimeError(f"memory replay mismatch: {scenario.scenario_id}")
        scenarios.append(
            {
                "scenario_id": scenario.scenario_id,
                "context_chars": len(context),
                "sections": sections,
                "candidate_count": len(memory.state["discovered_entities"]),
                "source_performance": backend.performance_snapshot(),
                "ground_truth_access": False,
                "replay": "PASS",
            }
        )
    if tuple(item["scenario_id"] for item in scenarios) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("zero-network scenario set mismatch")
    values = [int(item["context_chars"]) for item in scenarios]
    ordered = sorted(values)
    payload = {
        "execution": "ITB-E9",
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_count": len(scenarios),
        "openai_calls": 0,
        "judge_calls": 0,
        "ground_truth_in_runtime": False,
        "context_chars": {
            "min": min(values),
            "median": ordered[len(ordered) // 2],
            "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
            "max": max(values),
        },
        "scenarios": scenarios,
        "status": "PASS",
    }
    atomic_json_write(OUTPUT, payload)
    print(json.dumps({"status": "ITB_E9_ZERO_NETWORK_PASS", "scenarios": len(scenarios)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
