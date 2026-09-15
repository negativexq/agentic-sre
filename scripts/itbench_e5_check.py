#!/usr/bin/env python3
"""Zero-network E5 external contract and semantic snapshot qualification."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ExternalInvestigationRuntime,
    ITBenchExternalToolRegistry,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    build_observable_incident,
)
from packages.provider import FakeModelProvider, OpenAIProvider


def main() -> None:
    root = Path(".local/itbench-lite")
    dataset = ITBenchLiteDataset.open(root)
    scenarios = dataset.scenarios()
    if tuple(item.scenario_id for item in scenarios) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("pinned scenario order mismatch")
    timings: dict[str, list[float]] = {
        name: []
        for name in ("entity_search", "entity_context", "topology", "metrics", "logs", "traces")
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
            protocol_version="itbench_investigation_decision_v2",
        )
        request = runtime.build_request(incident, alerts, run_id=incident.incident_id)
        provider_payload = OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
        context = request.messages[-1].content
        if len(context) > 70_000 or "ground_truth" in context.casefold():
            raise RuntimeError(f"unsafe or oversized context: {scenario.scenario_id}")
        catalog = backend.observable_entities()
        entity = catalog[0] if catalog else None
        if entity is None:
            raise RuntimeError(f"no observable entity: {scenario.scenario_id}")
        canonical = f"{entity['namespace']}/{entity['kind']}/{entity['name']}"
        probes: dict[str, tuple[str, dict[str, Any]]] = {
            "entity_search": ("itbench_entity_search", {"entity": canonical, "limit": 5}),
            "entity_context": ("itbench_entity_context", {"entity": canonical, "limit": 5}),
            "topology": ("itbench_topology", {"limit": 5}),
            "metrics": ("itbench_metric_analysis", {"limit": 1}),
            "logs": ("itbench_logs", {"limit": 1}),
            "traces": ("itbench_trace_search", {"limit": 1}),
        }
        for key, (name, args) in probes.items():
            started = time.perf_counter()
            query_result = registry.invoke(name, args)
            timings[key].append((time.perf_counter() - started) * 1000)
            encoded = json.dumps(query_result, default=str)
            if len(encoded.encode()) > backend.max_bytes:
                raise RuntimeError(f"unbounded {name}: {scenario.scenario_id}")
        # Exercise the actual external terminal path without a provider.
        fake = FakeModelProvider(
            [
                {
                    "decision": "STOP",
                    "stop": {
                        "stop_reason": "insufficient_evidence",
                        "evidence_categories_considered": ["alerts"],
                        "entities_considered": [canonical],
                        "missing_evidence_categories": [],
                    },
                }
            ]
        )
        runtime_result = ExternalInvestigationRuntime(
            fake,
            registry.investigation_registry(),
            backend,
            protocol_version="itbench_investigation_decision_v2",
        ).run(incident, alerts)
        if runtime_result.terminal != "STOP":
            raise RuntimeError(f"fake external runtime failed: {scenario.scenario_id}")
        rows.append(
            {
                "scenario": scenario.scenario_id,
                "context_chars": len(context),
                "observable_entities": len(catalog),
                "tool_schema_bytes": len(
                    json.dumps(provider_payload.get("tools", []), default=str)
                ),
                "status": "PASS",
            }
        )
        print(f"qualified {scenario.scenario_id}", flush=True)
    summary = {
        "status": "PASS",
        "dataset_revision": ITBENCH_DATASET_REVISION,
        "scenario_count": len(rows),
        "scenarios": rows,
        "latency_ms": {
            key: {
                "median": statistics.median(values),
                "p95": sorted(values)[min(len(values) - 1, int(len(values) * 0.95))],
                "max": max(values),
            }
            for key, values in timings.items()
        },
        "openai_calls": 0,
        "ground_truth_access": False,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"ITBench E5 offline qualification: PASS ({len(rows)}/35, 0 OpenAI calls)")


if __name__ == "__main__":
    main()
