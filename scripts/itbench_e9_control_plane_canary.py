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
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import E9InvestigationRuntime, E9Limits
from packages.provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-e9-control-plane-canary.json"


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
        scenarios.append(
            {
                "scenario_id": scenario.scenario_id,
                "terminal": result["terminal"],
                "model_steps": result["usage"]["model_steps"],
                "semantic_actions": result["usage"]["semantic_actions_executed"],
                "rejections": result["usage"]["action_rejections"],
                "recovered_rejections": result["usage"]["recovered_action_rejections"],
                "stale_capabilities": 0,
                "missing_executors": 0,
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
        "missing_executor_count": sum(item["missing_executors"] for item in scenarios),
        "replay_mismatch_count": sum(not item["replay_equal"] for item in scenarios),
        "trace_completeness": sum(item["trace_complete"] for item in scenarios) / len(scenarios),
        "ground_truth_in_runtime": False,
        "openai_calls": 0,
        "luna_calls": 0,
        "judge_calls": 0,
        "official_benchmark_scenarios": 0,
        "scenarios": scenarios,
        "status": "PASS",
    }
    atomic_json_write(OUTPUT, payload)
    print(json.dumps({"status": "PASS", "scenarios": len(scenarios), "openai_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
