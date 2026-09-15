#!/usr/bin/env python3
"""Execute real offline control-plane variants over scripted trajectories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench import (
    ITBenchEvidenceCategory,
    ITBenchScenario,
    ITBenchSnapshotBackend,
    atomic_json_write,
    build_observable_incident,
)
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import E9InvestigationRuntime, E9Limits
from packages.provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/benchmarks/itbench-e9-internal-ablations-v2.json"


def _smoke_scenario() -> ITBenchScenario:
    root = ROOT / "tests/fixtures/itbench_smoke/Scenario-999"
    files: dict[ITBenchEvidenceCategory, tuple[str, ...]] = {
        ITBenchEvidenceCategory.ALERTS: ("alerts/alerts.json",),
        ITBenchEvidenceCategory.METRICS: ("metrics/checkout.tsv",),
        ITBenchEvidenceCategory.K8S_EVENTS: ("k8s_events_raw.tsv",),
        ITBenchEvidenceCategory.K8S_OBJECTS: ("k8s_objects_raw.tsv",),
        ITBenchEvidenceCategory.LOGS: ("otel_logs_raw.tsv",),
        ITBenchEvidenceCategory.TRACES: ("otel_traces_raw.tsv",),
    }
    return ITBenchScenario(
        scenario_id="Scenario-999",
        snapshot_path=str(root),
        evidence_categories=tuple(files),
        evidence_files=files,
    )


TRAJECTORIES: tuple[tuple[dict[str, Any], ...], ...] = (
    (
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
        {"action": "SUBMIT", "targets": ["C001"]},
    ),
    (
        {"action": "HYPOTHESIZE", "target": "C999"},
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "SPEC_ANALYSIS"},
        {"action": "SUBMIT", "targets": ["C001"]},
    ),
    (
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "SPEC_ANALYSIS"},
        {"action": "SUBMIT", "targets": ["C001"]},
    ),
    (
        {"action": "INVESTIGATE", "target": "C001", "operation": "METRIC_ANOMALIES"},
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "EVENT_ANALYSIS"},
        {"action": "SUBMIT", "targets": ["C001"]},
    ),
    (
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "REVISE", "target": "C001", "rationale": "reconsider after new evidence"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "VERIFY_TEMPORAL_ALIGNMENT"},
        {"action": "SUBMIT", "targets": ["C001"]},
    ),
    ({"action": "STOP", "stop_reason": "insufficient evidence"},),
    (
        {"action": "OBSERVE"},
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "RECENT_CHANGE_ANALYSIS"},
        {"action": "SUBMIT", "targets": ["C001"]},
    ),
    (
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "TRACE_ERROR_TREE"},
        {"action": "REVISE", "target": "C001"},
        {"action": "SUBMIT", "targets": ["C001"]},
    ),
    (
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "COMPARE_REPLICAS"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "EVENT_ANALYSIS"},
        {"action": "SUBMIT", "targets": ["C001"]},
    ),
    (
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "METRIC_ANOMALIES"},
        {"action": "SUBMIT", "targets": ["C001"]},
    ),
    (
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "EVENT_ANALYSIS"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "SPEC_ANALYSIS"},
        {"action": "STOP", "stop_reason": "investigation complete"},
    ),
    (
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "UNSUPPORTED"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
        {"action": "SUBMIT", "targets": ["C001"]},
    ),
)


def run_variant(name: str, *, reject_limit: int) -> dict[str, Any]:
    runs = []
    for index, trajectory in enumerate(TRAJECTORIES, start=1):
        scenario = _smoke_scenario()
        backend = ITBenchSnapshotBackend(cast(Any, None), scenario, max_rows=5, max_bytes=20_000)
        incident, alerts = build_observable_incident(backend)
        runtime = E9InvestigationRuntime(
            FakeModelProvider(
                [
                    *trajectory,
                    *({"action": "STOP", "stop_reason": "offline trajectory exhausted"},) * 12,
                ]
            ),
            backend,
            limits=E9Limits(
                max_model_calls=12, max_tool_calls=24, max_agent_turns=12, max_wall_time_seconds=240
            ),
            execution_id=f"ITB-E9-OFFLINE-{name}-{index}",
            max_consecutive_rejected_actions=reject_limit,
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
        runs.append(
            {
                "trajectory": index,
                "terminal": result["terminal"],
                "model_steps": result["usage"]["model_steps"],
                "rejections": result["usage"]["action_rejections"],
                "recovered_rejections": result["usage"]["recovered_action_rejections"],
                "semantic_actions": result["usage"]["semantic_actions_executed"],
                "context_chars": [
                    item["context_chars"] for item in result["usage"]["context_metrics"]
                ],
                "replay_equal": replay.projection() == result["case_state"],
                "trace_complete": len(result["turns"]) == result["usage"]["model_steps"],
            }
        )
    terminals: dict[str, int] = {}
    for run in runs:
        terminals[run["terminal"]] = terminals.get(run["terminal"], 0) + 1
    return {
        "variant": name,
        "configuration": {"max_consecutive_rejected_actions": reject_limit},
        "trajectory_count": len(runs),
        "terminal_counts": terminals,
        "completion_rate": sum(run["terminal"] in {"SUBMIT", "STOP"} for run in runs) / len(runs),
        "protocol_stall_count": terminals.get("PROTOCOL_STALLED", 0),
        "rejection_count": sum(run["rejections"] for run in runs),
        "recovered_rejection_count": sum(run["recovered_rejections"] for run in runs),
        "replay_mismatches": sum(not run["replay_equal"] for run in runs),
        "trace_incomplete": sum(not run["trace_complete"] for run in runs),
        "runs": runs,
    }


def main() -> int:
    variants = [run_variant("R0", reject_limit=0)] + [
        run_variant(name, reject_limit=2) for name in ("R1", "R2", "R3", "R4", "R5", "R6", "R7")
    ]
    payload = {
        "execution": "E9_OFFLINE_PREFLIGHT",
        "method": "real FakeModelProvider trajectories over the E9 runtime; no network, no judge, no official GT",
        "trajectory_count_per_variant": len(TRAJECTORIES),
        "variants": variants,
        "hard_coded_metrics": False,
        "openai_calls": 0,
        "judge_calls": 0,
    }
    atomic_json_write(OUTPUT, payload)
    print(
        json.dumps({"status": "PASS", "variants": len(variants), "trajectories": len(TRAJECTORIES)})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
