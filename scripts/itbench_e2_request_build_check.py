#!/usr/bin/env python3
"""Cheap zero-network request-build qualification for all pinned E2 scenarios."""

from __future__ import annotations

import json
from pathlib import Path

from packages.evals.itbench import (
    ITBENCH_SCENARIO_IDS,
    ExternalInvestigationRuntime,
    ITBenchExternalToolRegistry,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    build_observable_incident,
)
from packages.investigation.contracts import InvestigationLimits
from packages.provider import FakeModelProvider, OpenAIProvider

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local" / "itbench-lite"
EXPECTED_FUNCTIONS = {
    "request_itbench_tools",
    "submit_itbench_diagnosis",
    "stop_itbench_investigation",
}


def main() -> int:
    """Build every external request and wire payload without provider construction."""
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    scenarios = dataset.scenarios()
    if tuple(item.scenario_id for item in scenarios) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("pinned scenario order mismatch")
    rows: list[dict[str, int | str | bool]] = []
    for scenario in scenarios:
        backend = ITBenchSnapshotBackend(dataset, scenario)
        external_registry = ITBenchExternalToolRegistry(backend)
        incident, alerts = build_observable_incident(backend)
        runtime = ExternalInvestigationRuntime(
            FakeModelProvider([]),
            external_registry.investigation_registry(),
            backend,
            limits=InvestigationLimits(
                max_model_calls=5,
                max_tool_calls=12,
                max_agent_turns=5,
                max_wall_time_seconds=180,
            ),
            model="gpt-5.6-luna",
            reasoning_effort="none",
        )
        request = runtime.build_request(incident, alerts, run_id=incident.incident_id)
        payload = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
        function_names = {item["name"] for item in payload.get("tools", [])}
        if function_names != EXPECTED_FUNCTIONS:
            raise RuntimeError(f"external function set mismatch: {scenario.scenario_id}")
        context = request.messages[-1].content
        if "ground_truth" in context.casefold():
            raise RuntimeError(f"ground-truth context leakage: {scenario.scenario_id}")
        rows.append(
            {
                "scenario_id": scenario.scenario_id,
                "context_chars": len(context),
                "context_bytes": len(context.encode()),
                "tool_schema_bytes": len(json.dumps(payload["tools"], sort_keys=True).encode()),
                "request_build": True,
                "provider_request_build": True,
            }
        )
    if len(rows) != 35:
        raise RuntimeError(f"expected 35 scenarios, got {len(rows)}")
    print(json.dumps({"status": "PASS", "scenarios": rows, "openai_calls": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
