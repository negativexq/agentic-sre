#!/usr/bin/env python3
"""Cheap, zero-network E7 contract and snapshot qualification."""

from __future__ import annotations

import json
import time
from pathlib import Path

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

REPORT = Path("docs/benchmarks/itbench-e7-zero-network-qualification.json")


def main() -> None:
    dataset = ITBenchLiteDataset.open(Path(".local/itbench-lite"))
    scenarios = dataset.scenarios()
    if tuple(item.scenario_id for item in scenarios) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("pinned scenario order mismatch")
    rows = []
    for scenario in scenarios:
        started = time.perf_counter()
        backend = ITBenchSnapshotBackend(dataset, scenario)
        registry = ITBenchExternalToolRegistry(backend)
        incident, alerts = build_observable_incident(backend)
        runtime = ExternalInvestigationRuntime(
            FakeModelProvider([]),
            registry.investigation_registry(),
            backend,
            protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V3,
        )
        request = runtime.build_request(
            incident,
            alerts,
            run_id=incident.incident_id,
            candidate_entities=backend.candidate_entities(limit=10),
        )
        context = request.messages[-1].content
        forbidden = (
            "ground_truth",
            "default_topology",
            "order-service",
            "payment-service",
            "order-worker",
        )
        if any(value in context.casefold() for value in forbidden):
            raise RuntimeError(f"unsafe external context: {scenario.scenario_id}")
        OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
        entities = backend.observable_entities()
        if not entities:
            raise RuntimeError(f"empty entity catalog: {scenario.scenario_id}")
        entity = entities[0]
        canonical = f"{entity['namespace']}/{entity['kind']}/{entity['name']}"
        search = registry.invoke("itbench_entity_search", {"entity": canonical, "limit": 5})
        if not any(
            item.get("namespace") == entity["namespace"]
            and item.get("kind") == entity["kind"]
            and item.get("name") == entity["name"]
            for item in search["records"]
        ):
            raise RuntimeError(f"entity search round trip failed: {scenario.scenario_id}")
        context_result = registry.invoke(
            "itbench_entity_context", {"entity": canonical, "limit": 5}
        )
        if context_result.get("entity") != canonical:
            raise RuntimeError(f"entity context mismatch: {scenario.scenario_id}")
        for tool, args in (
            ("itbench_topology", {"pattern": "definitely-nonexistent", "limit": 5}),
            ("itbench_metric_analysis", {"limit": 1}),
            ("itbench_logs", {"limit": 1}),
            ("itbench_trace_search", {"limit": 1}),
            ("itbench_kubernetes_events", {"limit": 1}),
            ("itbench_kubernetes_objects", {"limit": 1}),
        ):
            value = registry.invoke(tool, args)
            if len(json.dumps(value, default=str).encode()) > backend.max_bytes:
                raise RuntimeError(f"unbounded output: {scenario.scenario_id}/{tool}")
        rows.append(
            {
                "scenario": scenario.scenario_id,
                "context_chars": len(context),
                "entities": len(entities),
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "status": "PASS",
            }
        )
    report = {
        "execution": "ITB-E7-pre",
        "status": "PASS",
        "scenario_count": len(rows),
        "scenarios": rows,
        "openai_calls": 0,
        "ground_truth_access": False,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
