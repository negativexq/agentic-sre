#!/usr/bin/env python3
"""Zero-network E8 context, contract, and snapshot qualification."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from packages.evals.itbench import (
    ITBENCH_EXTERNAL_PROTOCOL_V4,
    ITBENCH_SCENARIO_IDS,
    ExternalInvestigationRuntime,
    ITBenchExternalToolRegistry,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    build_observable_incident,
)
from packages.provider import FakeModelProvider, OpenAIProvider

CONTEXT_REPORT = Path("docs/benchmarks/itbench-e8-context-composition.json")
QUALIFICATION_REPORT = Path("docs/benchmarks/itbench-e8-zero-network-qualification.json")


def _context_composition(context: str) -> dict[str, int]:
    payload = json.loads(context)
    result = {
        key: len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))
        for key, value in payload.items()
        if key not in {"benchmark", "domain", "context_version"}
    }
    result["total"] = len(context)
    return result


def main() -> None:
    dataset = ITBenchLiteDataset.open(Path(".local/itbench-lite"))
    scenarios = dataset.scenarios()
    if tuple(item.scenario_id for item in scenarios) != ITBENCH_SCENARIO_IDS:
        raise RuntimeError("pinned scenario order mismatch")
    rows: list[dict[str, Any]] = []
    durations: dict[str, list[float]] = {
        name: [] for name in ("context", "entity_context", "metrics", "logs", "traces")
    }
    for scenario in scenarios:
        started = time.perf_counter()
        backend = ITBenchSnapshotBackend(dataset, scenario)
        registry = ITBenchExternalToolRegistry(backend, contract_version="v4")
        incident, alerts = build_observable_incident(backend)
        runtime = ExternalInvestigationRuntime(
            FakeModelProvider([]),
            registry.investigation_registry(),
            backend,
            protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V4,
        )
        candidates = backend.candidate_entities(limit=10)
        context_started = time.perf_counter()
        request = runtime.build_request(
            incident,
            alerts,
            run_id=incident.incident_id,
            candidate_entities=candidates,
        )
        durations["context"].append((time.perf_counter() - context_started) * 1000)
        context = request.messages[-1].content
        payload = json.loads(context)
        if payload.get("context_version") != "itbench_external_context_v3":
            raise RuntimeError(f"wrong context version: {scenario.scenario_id}")
        if "ground_truth" in context.casefold() or "root-cause answer" in context.casefold():
            raise RuntimeError(f"ground truth leaked into context: {scenario.scenario_id}")
        OpenAIProvider.__new__(OpenAIProvider)._request_parameters(request)
        entities = backend.observable_entities()
        if not entities:
            raise RuntimeError(f"empty entity catalog: {scenario.scenario_id}")
        entity = entities[0]
        canonical = f"{entity['namespace']}/{entity['kind']}/{entity['name']}"
        probes = (
            ("entity_search", "itbench_entity_search", {"entity": canonical, "limit": 5}),
            ("entity_context", "itbench_entity_context", {"entity": canonical, "limit": 5}),
            ("topology", "itbench_topology", {"entity": canonical, "limit": 20}),
            ("metrics", "itbench_metric_analysis", {"limit": 1}),
            ("logs", "itbench_logs", {"contains": "error", "limit": 1}),
            ("traces", "itbench_trace_search", {"limit": 1}),
            ("events", "itbench_kubernetes_events", {"entity": canonical, "limit": 5}),
            ("objects", "itbench_kubernetes_objects", {"entity": canonical, "limit": 5}),
        )
        probe_status: dict[str, str] = {}
        for category, tool, arguments in probes:
            probe_started = time.perf_counter()
            result = registry.invoke(tool, arguments)
            if category in durations:
                durations[category].append((time.perf_counter() - probe_started) * 1000)
            encoded = json.dumps(result, default=str)
            if len(encoded.encode()) > backend.max_bytes:
                raise RuntimeError(f"unbounded result: {scenario.scenario_id}/{tool}")
            probe_status[category] = "PASS"
        rows.append(
            {
                "scenario": scenario.scenario_id,
                "context_chars": len(context),
                "context_composition": _context_composition(context),
                "observable_entities": len(entities),
                "candidate_shortlist": len(candidates),
                "probe_status": probe_status,
                "backend_performance": backend.performance_snapshot(),
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "status": "PASS",
            }
        )
        print(f"qualified {scenario.scenario_id}", flush=True)
    context_values = [int(row["context_chars"]) for row in rows]
    composition_keys = sorted(rows[0]["context_composition"])
    composition = {
        key: {
            "min": min(int(row["context_composition"].get(key, 0)) for row in rows),
            "median": statistics.median(
                int(row["context_composition"].get(key, 0)) for row in rows
            ),
            "p95": sorted(int(row["context_composition"].get(key, 0)) for row in rows)[
                min(34, int(35 * 0.95))
            ],
            "max": max(int(row["context_composition"].get(key, 0)) for row in rows),
        }
        for key in composition_keys
    }
    context_report = {
        "execution": "ITB-E8-pre",
        "context_version": "itbench_external_context_v3",
        "scenario_count": len(rows),
        "total_chars": {
            "min": min(context_values),
            "median": statistics.median(context_values),
            "p95": sorted(context_values)[min(34, int(35 * 0.95))],
            "max": max(context_values),
        },
        "sections": composition,
        "scenario_105": next(row for row in rows if row["scenario"] == "Scenario-105"),
        "ground_truth_access": False,
        "openai_calls": 0,
    }
    report = {
        "execution": "ITB-E8-pre",
        "status": "PASS",
        "scenario_count": len(rows),
        "scenarios": rows,
        "latency_ms": {
            key: {
                "median": statistics.median(values),
                "p95": sorted(values)[min(len(values) - 1, int(len(values) * 0.95))],
                "max": max(values),
            }
            for key, values in durations.items()
            if values
        },
        "openai_calls": 0,
        "judge_calls": 0,
        "ground_truth_access": False,
        "protocol": ITBENCH_EXTERNAL_PROTOCOL_V4,
    }
    CONTEXT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT_REPORT.write_text(
        json.dumps(context_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    QUALIFICATION_REPORT.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
