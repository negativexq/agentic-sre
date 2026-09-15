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
from packages.evals.itbench.e9_control import E9ControlPlaneVariant
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import E9InvestigationRuntime, E9Limits
from packages.provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/benchmarks/itbench-control-plane-ablation-v3.json"


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
    (
        {"action": "HYPOTHESIZE", "target": "C001"},
        {"action": "INVESTIGATE", "target": "C001", "operation": "TOPOLOGY_ANALYSIS"},
        {"action": "STOP", "stop_reason": "discovery trajectory complete"},
    ),
)


def run_variant(name: str, variant: E9ControlPlaneVariant) -> dict[str, Any]:
    runs = []
    for index, trajectory in enumerate(TRAJECTORIES, start=1):
        scenario = _smoke_scenario()
        backend = ITBenchSnapshotBackend(cast(Any, None), scenario, max_rows=5, max_bytes=20_000)
        incident, alerts = build_observable_incident(backend)
        provider = FakeModelProvider(
            [
                *trajectory,
                *({"action": "STOP", "stop_reason": "offline trajectory exhausted"},) * 12,
            ]
        )
        runtime = E9InvestigationRuntime(
            provider,
            backend,
            limits=E9Limits(
                max_model_calls=12, max_tool_calls=24, max_agent_turns=12, max_wall_time_seconds=240
            ),
            execution_id=f"ITB-E9-OFFLINE-{name}-{index}",
            max_consecutive_rejected_actions=2 if variant.recoverable_rejections else 0,
            variant=variant,
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
                "provider_schema_chars": sum(
                    len(json.dumps(request.response_schema, sort_keys=True))
                    for request in provider.requests
                ),
                "exposed_action_counts": [
                    len(request.allowed_v5_actions or ()) + len(request.allowed_decisions or ())
                    for request in provider.requests
                ],
                "exposed_operation_counts": [
                    len(request.allowed_v5_operations or ()) for request in provider.requests
                ],
                "rejection_feedback_visible": any(
                    '"last_rejection"' in str(request.messages[-1].content)
                    for request in provider.requests
                ),
                "synthetic_correct": result["submitted_entities"] == ["otel-demo/Service/checkout"],
                "discovered_candidate_count": len(
                    result["case_state"].get("discovered_entities", {})
                ),
            }
        )
    terminals: dict[str, int] = {}
    for run in runs:
        terminals[run["terminal"]] = terminals.get(run["terminal"], 0) + 1
    return {
        "variant": name,
        "configuration": {
            "max_consecutive_rejected_actions": 2 if variant.recoverable_rejections else 0
        },
        "trajectory_count": len(runs),
        "terminal_counts": terminals,
        "completion_rate": sum(run["terminal"] in {"SUBMIT", "STOP"} for run in runs) / len(runs),
        "protocol_stall_count": terminals.get("PROTOCOL_STALLED", 0),
        "rejection_count": sum(run["rejections"] for run in runs),
        "recovered_rejection_count": sum(run["recovered_rejections"] for run in runs),
        "mean_discovered_candidates": sum(run["discovered_candidate_count"] for run in runs)
        / len(runs),
        "synthetic_correct_count": sum(run["synthetic_correct"] for run in runs),
        "mean_model_steps": sum(run["model_steps"] for run in runs) / len(runs),
        "mean_context_chars": sum(sum(run["context_chars"]) for run in runs) / len(runs),
        "mean_exposed_actions": sum(sum(run["exposed_action_counts"]) for run in runs)
        / max(1, sum(len(run["exposed_action_counts"]) for run in runs)),
        "mean_exposed_operations": sum(sum(run["exposed_operation_counts"]) for run in runs)
        / max(1, sum(len(run["exposed_operation_counts"]) for run in runs)),
        "rejection_feedback_visible_runs": sum(run["rejection_feedback_visible"] for run in runs),
        "configuration_hash": variant.config_hash(),
        "feature_flags": variant.as_dict(),
        "replay_mismatches": sum(not run["replay_equal"] for run in runs),
        "trace_incomplete": sum(not run["trace_complete"] for run in runs),
        "runs": runs,
    }


def main() -> int:
    variant_pairs = [
        ("R0", E9ControlPlaneVariant(False, False, False, False, False, False, False)),
        ("R1", E9ControlPlaneVariant(True, False, False, False, False, False, False)),
        ("R2", E9ControlPlaneVariant(True, True, False, False, False, False, False)),
        ("R3", E9ControlPlaneVariant(True, True, True, False, False, False, False)),
        ("R4", E9ControlPlaneVariant(True, True, True, True, False, False, False)),
        ("R5", E9ControlPlaneVariant(True, True, True, True, True, False, False)),
        ("R6", E9ControlPlaneVariant(True, True, True, True, True, True, False)),
        ("R7", E9ControlPlaneVariant(True, True, True, True, True, True, True)),
    ]
    variants = [run_variant(name, variant) for name, variant in variant_pairs]
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
