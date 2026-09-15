#!/usr/bin/env python3
"""Offline simulated future smoke using only FakeModelProvider."""

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
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-control-plane-simulated-smoke.json"


def _run(dataset: ITBenchLiteDataset, responses: list[dict[str, Any]], name: str) -> dict[str, Any]:
    scenario = next(iter(dataset.scenarios()))
    backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
    incident, alerts = build_observable_incident(backend)
    provider = FakeModelProvider(responses)
    result = E9InvestigationRuntime(
        provider, backend, limits=E9Limits(), execution_id=f"OFFLINE-{name}"
    ).run(incident, alerts)
    return {
        "name": name,
        "terminal": result["terminal"],
        "model_steps": result["usage"]["model_steps"],
        "semantic_actions": result["usage"]["semantic_actions_executed"],
        "action_rejections": result["usage"]["action_rejections"],
        "recovered_rejections": result["usage"]["recovered_action_rejections"],
        "evidence": result["evidence"],
        "turns": result["turns"],
        "openai_calls": 0,
        "judge_calls": 0,
    }


def main() -> int:
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    target = "C001"
    cases = [
        _run(
            dataset,
            [
                {
                    "action": "HYPOTHESIZE",
                    "target": target,
                    "rationale": "form a bounded hypothesis",
                },
                {"action": "INVESTIGATE", "target": target, "operation": "ENTITY_CONTEXT"},
                {"action": "INVESTIGATE", "target": target, "operation": "SPEC_ANALYSIS"},
                {"action": "SUBMIT", "targets": [target]},
            ],
            "SIMULATED_SMOKE",
        ),
        _run(
            dataset,
            [
                {"action": "HYPOTHESIZE", "target": "C999"},
                {"action": "HYPOTHESIZE", "target": target},
                {"action": "INVESTIGATE", "target": target, "operation": "ENTITY_CONTEXT"},
                {"action": "INVESTIGATE", "target": target, "operation": "ENTITY_CONTEXT"},
                {"action": "INVESTIGATE", "target": target, "operation": "SPEC_ANALYSIS"},
                {"action": "SUBMIT", "targets": [target]},
            ],
            "ADVERSARIAL_SIMULATED_SMOKE",
        ),
    ]
    payload = {
        "execution": "E9_OFFLINE_PREFLIGHT",
        "purpose": "OFFLINE_SIMULATED_SMOKE",
        "provider": "fake",
        "ground_truth_used": False,
        "openai_calls": 0,
        "luna_calls": 0,
        "judge_calls": 0,
        "cases": cases,
        "status": "PASS"
        if all(case["terminal"] in {"SUBMIT", "STOP"} for case in cases)
        else "FAIL",
    }
    atomic_json_write(OUTPUT, payload)
    print(json.dumps({"status": payload["status"], "cases": len(cases), "openai_calls": 0}))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
