#!/usr/bin/env python3
"""Zero-network E6 qualification over all pinned observable snapshots."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBENCH_EXTERNAL_PROTOCOL_V3,
    ITBENCH_SCENARIO_IDS,
    ExternalInvestigationRuntime,
    ITBenchExternalToolRegistry,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    build_observable_incident,
)
from packages.provider import FakeModelProvider, OpenAIProvider

REPORT = Path("docs/benchmarks/itbench-e6-zero-network-qualification.json")


def main() -> None:
    dataset = ITBenchLiteDataset.open(Path(".local/itbench-lite"))
    scenarios = dataset.scenarios()
    if tuple(item.scenario_id for item in scenarios) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("pinned scenario order mismatch")
    timings: dict[str, list[float]] = {
        key: []
        for key in ("entity_search", "entity_context", "topology", "metrics", "logs", "traces")
    }
    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        backend = ITBenchSnapshotBackend(dataset, scenario)
        registry = ITBenchExternalToolRegistry(backend)
        incident, alerts = build_observable_incident(backend)
        runtime = ExternalInvestigationRuntime(
            FakeModelProvider([]),
            registry.investigation_registry(),
            backend,
            protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V3,
        )
        request = runtime.build_request(incident, alerts, run_id=incident.incident_id)
        OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
        context = request.messages[-1].content
        if len(context) > 70_000 or "ground_truth" in context.casefold():
            raise RuntimeError(f"unsafe context: {scenario.scenario_id}")
        catalog = backend.observable_entities()
        if not catalog:
            raise RuntimeError(f"empty entity catalog: {scenario.scenario_id}")
        entity = catalog[0]
        canonical = f"{entity['namespace']}/{entity['kind']}/{entity['name']}"
        probes: dict[str, tuple[str, dict[str, Any]]] = {
            "entity_search": ("itbench_entity_search", {"entity": canonical, "limit": 5}),
            "entity_context": ("itbench_entity_context", {"entity": canonical, "limit": 5}),
            "topology": ("itbench_topology", {"limit": 5}),
            "metrics": ("itbench_metric_analysis", {"limit": 1}),
            "logs": ("itbench_logs", {"limit": 1}),
            "traces": ("itbench_trace_search", {"limit": 1}),
        }
        for key, (tool, args) in probes.items():
            started = time.perf_counter()
            value = registry.invoke(tool, args)
            timings[key].append((time.perf_counter() - started) * 1000)
            if len(json.dumps(value, default=str).encode()) > backend.max_bytes:
                raise RuntimeError(f"unbounded output: {scenario.scenario_id}/{tool}")
        rows.append(
            {
                "scenario": scenario.scenario_id,
                "context_chars": len(context),
                "observable_entities": len(catalog),
                "entity_round_trip": True,
                "status": "PASS",
            }
        )
        print(f"qualified {scenario.scenario_id}", flush=True)
    latency = {
        key: {
            "median": statistics.median(values),
            "p95": sorted(values)[min(len(values) - 1, int(len(values) * 0.95))],
            "max": max(values),
        }
        for key, values in timings.items()
    }
    report = {
        "status": "PASS",
        "scenario_count": len(rows),
        "scenarios": rows,
        "latency_ms": latency,
        "openai_calls": 0,
        "ground_truth_access": False,
        "metric_engine": "single_pass_matching_aggregate_and_bounded_samples",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"ITBench E6 offline qualification: PASS ({len(rows)}/35, 0 OpenAI calls)")


if __name__ == "__main__":
    main()
