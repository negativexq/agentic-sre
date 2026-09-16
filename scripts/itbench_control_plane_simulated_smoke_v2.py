#!/usr/bin/env python3
"""Offline interface smoke v2 using FakeModelProvider only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    atomic_json_write,
    build_observable_incident,
)
from packages.evals.itbench.e9_runtime import E9InvestigationRuntime, E9Limits
from packages.provider import FakeModelProvider

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/benchmarks/itbench-control-plane-simulated-smoke-v2.json"


def run_case(
    dataset: ITBenchLiteDataset, responses: list[dict[str, Any]], name: str
) -> dict[str, Any]:
    scenario = next(iter(dataset.scenarios()))
    backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
    incident, alerts = build_observable_incident(backend)
    result = E9InvestigationRuntime(
        FakeModelProvider(responses), backend, limits=E9Limits(), execution_id=f"OFFLINE-{name}"
    ).run(incident, alerts)
    return {
        "name": name,
        "terminal": result["terminal"],
        "model_steps": result["usage"]["model_steps"],
        "semantic_actions": result["usage"]["semantic_actions_executed"],
        "action_rejections": result["usage"]["action_rejections"],
        "recovered_rejections": result["usage"]["recovered_action_rejections"],
        "turns": result["turns"],
        "replay_events": len(result["events"]),
        "trace_complete": len(result["turns"]) == result["usage"]["model_steps"],
    }


def main() -> int:
    dataset = ITBenchLiteDataset.open(ROOT / ".local/itbench-lite")
    normal = run_case(
        dataset,
        [
            {"action": "HYPOTHESIZE", "target": "C001", "rationale": "select seed"},
            {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
            {"action": "SUBMIT", "targets": ["C001"]},
        ],
        "NORMAL",
    )
    adversarial = run_case(
        dataset,
        [
            {"action": "HYPOTHESIZE", "target": "C999"},
            {"action": "HYPOTHESIZE", "target": "C001"},
            {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
            {"action": "INVESTIGATE", "target": "C001", "operation": "ENTITY_CONTEXT"},
            {"action": "INVESTIGATE", "target": "C001", "operation": "SPEC_ANALYSIS"},
            {"action": "SUBMIT", "targets": ["C001"]},
        ],
        "ADVERSARIAL",
    )
    payload = {
        "execution": "E9_OFFLINE_INTERFACE_HARDENING",
        "purpose": "SIMULATED_SMOKE_V2",
        "provider": "fake",
        "ground_truth_used": False,
        "model_calls": 0,
        "openai_outbound_attempts": 0,
        "cases": [normal, adversarial],
        "status": "PASS"
        if all(
            case["terminal"] in {"SUBMIT", "STOP"} and case["trace_complete"]
            for case in (normal, adversarial)
        )
        else "FAIL",
    }
    atomic_json_write(OUTPUT, payload)
    print(json.dumps({"status": payload["status"], "cases": 2, "model_calls": 0}))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
