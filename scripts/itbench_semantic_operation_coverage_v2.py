#!/usr/bin/env python3
"""Offline semantic capability coverage; never constructs a live provider."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
from typing import Any

from packages.evals.itbench import (
    ITBENCH_SCENARIO_IDS,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    atomic_json_write,
    build_observable_incident,
)
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_semantic import (
    E9_SEMANTIC_OPERATIONS,
    E9SemanticOperations,
    SemanticCapabilityResolver,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-semantic-operation-coverage-v2.json"
MARKDOWN = ROOT / "docs/benchmarks/itbench-semantic-operation-coverage-v2.md"


def _seed(backend: ITBenchSnapshotBackend) -> str | None:
    candidates = backend.candidate_entities(limit=1)
    return str(candidates[0]["canonical"]) if candidates else None


def _classify(summary: Any) -> tuple[bool, bool, bool]:
    """Return useful, unavailable, inconclusive from honest semantic fields."""
    if not isinstance(summary, dict):
        return False, False, False
    unavailable = summary.get("available") is False or any(
        summary.get(key) is False
        for key in ("change_history_available", "error_tree_available", "comparison_available")
    )
    inconclusive = (
        summary.get("causal_temporal_assessment") == "INCONCLUSIVE"
        or summary.get("assessment") == "INCONCLUSIVE"
    )
    useful = False
    if not unavailable and not inconclusive:
        useful = bool(summary.get("items") or summary.get("edges") or summary.get("records"))
        if "specs" in summary:
            useful = bool(summary.get("specs"))
        if "comparison_available" in summary:
            useful = bool(
                summary.get("comparison_available")
                and (summary.get("spec_differences") or summary.get("event_differences"))
            )
        if "temporal_relation" in summary:
            useful = summary.get("temporal_relation") not in {None, "UNKNOWN"}
        if "changes" in summary:
            useful = bool(summary.get("change_history_available") and summary.get("changes"))
    return useful, unavailable, inconclusive


def _scenario(dataset: ITBenchLiteDataset, scenario: Any) -> list[dict[str, Any]]:
    backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
    incident, _ = build_observable_incident(backend)
    canonical = _seed(backend)
    memory = E9CaseMemory(execution_id="E9-COVERAGE-V2", scenario_id=scenario.scenario_id)
    if canonical:
        memory.discover_entities(({"canonical": canonical},))
    target = "C001" if canonical else None
    resolver = SemanticCapabilityResolver(backend, memory, incident)
    availability = resolver.resolve(target)
    operations = E9SemanticOperations(backend, memory, incident)
    rows: list[dict[str, Any]] = []
    for operation in E9_SEMANTIC_OPERATIONS:
        target_for_operation = (
            target
            if operation
            in {
                "ENTITY_CONTEXT",
                "METRIC_ANOMALIES",
                "TRACE_ERROR_TREE",
                "SPEC_ANALYSIS",
                "COMPARE_REPLICAS",
                "VERIFY_TEMPORAL_ALIGNMENT",
            }
            else None
        )
        try:
            result = operations.execute(operation, target_for_operation, 1)
            summary = result["summary"]
            useful, unavailable, inconclusive = _classify(summary)
            rows.append(
                {
                    "operation": operation,
                    "available": bool(availability[operation]["available"]),
                    "useful": useful,
                    "unavailable": unavailable,
                    "inconclusive": inconclusive,
                    "structured": isinstance(summary, (dict, list)),
                    "chars": len(json.dumps(summary, ensure_ascii=False, default=str)),
                    "error": None,
                }
            )
        except (TypeError, ValueError, KeyError) as error:
            rows.append(
                {
                    "operation": operation,
                    "available": False,
                    "useful": False,
                    "unavailable": False,
                    "inconclusive": False,
                    "structured": False,
                    "chars": 0,
                    "error": str(error)[:200],
                }
            )
    return rows


def main() -> int:
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    scenarios = list(dataset.scenarios())
    collected: list[list[dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        collected.extend(executor.map(lambda item: _scenario(dataset, item), scenarios))
    rows: list[dict[str, Any]] = []
    for operation in E9_SEMANTIC_OPERATIONS:
        values = [item for result in collected for item in result if item["operation"] == operation]
        chars = [int(item["chars"]) for item in values]
        rows.append(
            {
                "operation": operation,
                "scenarios": len(values),
                "available": sum(bool(item["available"]) for item in values),
                "useful": sum(bool(item["useful"]) for item in values),
                "unavailable": sum(bool(item["unavailable"]) for item in values),
                "inconclusive": sum(bool(item["inconclusive"]) for item in values),
                "errors": sum(item["error"] is not None for item in values),
                "provider_exposed_scenarios": sum(bool(item["available"]) for item in values),
                "median_output_chars": int(median(chars)) if chars else 0,
                "p95_output_chars": sorted(chars)[max(0, int(len(chars) * 0.95) - 1)]
                if chars
                else 0,
            }
        )
    payload = {
        "execution": "E9_OFFLINE_PREFLIGHT",
        "purpose": "SEMANTIC_OPERATION_COVERAGE_V2",
        "dataset_scenarios": list(ITBENCH_SCENARIO_IDS),
        "ground_truth_used": False,
        "openai_calls": 0,
        "luna_calls": 0,
        "judge_calls": 0,
        "operations": rows,
    }
    atomic_json_write(OUTPUT, payload)
    lines = [
        "# ITBench semantic operation coverage v2 (offline)",
        "",
        "Capability availability is observable-snapshot availability, not RCA quality.",
        "",
        "| Operation | Scenarios | Available/provider | Useful | Unavailable | Inconclusive | Errors | Median | P95 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {r['operation']} | {r['scenarios']} | {r['available']} | {r['useful']} | {r['unavailable']} | {r['inconclusive']} | {r['errors']} | {r['median_output_chars']} | {r['p95_output_chars']} |"
        for r in rows
    ]
    MARKDOWN.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "PASS",
                "operations": len(rows),
                "scenarios": len(scenarios),
                "openai_calls": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
