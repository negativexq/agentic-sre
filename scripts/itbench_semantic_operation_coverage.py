#!/usr/bin/env python3
"""Offline honesty/coverage matrix for the E9 semantic operation facade."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
from typing import Any

from packages.evals.itbench import (
    ITBENCH_SCENARIO_IDS,
    ITBenchEvidenceCategory,
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    atomic_json_write,
    build_observable_incident,
)
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_semantic import E9_SEMANTIC_OPERATIONS, E9SemanticOperations

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-semantic-operation-coverage.json"
MARKDOWN = ROOT / "docs/benchmarks/itbench-semantic-operation-coverage.md"


def _first_observable_entity(backend: ITBenchSnapshotBackend) -> str | None:
    """Use only the bounded object sample for coverage targets."""
    for item in backend.records(ITBenchEvidenceCategory.K8S_OBJECTS):
        record = item.get("record", {})
        body = record.get("Body") if isinstance(record, dict) else None
        if not isinstance(body, str):
            continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            continue
        metadata = parsed.get("metadata", {}) if isinstance(parsed, dict) else {}
        if isinstance(parsed, dict) and isinstance(metadata, dict):
            if isinstance(parsed.get("kind"), str) and isinstance(metadata.get("name"), str):
                return (
                    f"{metadata.get('namespace', '_cluster')}/{parsed['kind']}/{metadata['name']}"
                )
    return None


def _run_scenario(dataset: ITBenchLiteDataset, scenario: Any) -> list[dict[str, Any]]:
    backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
    incident, _ = build_observable_incident(backend)
    seed_entity = _first_observable_entity(backend)
    results: list[dict[str, Any]] = []
    for operation in E9_SEMANTIC_OPERATIONS:
        memory = E9CaseMemory(execution_id="E9-SEMANTIC-COVERAGE", scenario_id=scenario.scenario_id)
        if seed_entity:
            memory.discover_entities(({"canonical": seed_entity},))
        target = (
            None
            if operation in {"INCIDENT_OVERVIEW", "ALERT_ANALYSIS", "TOPOLOGY_ANALYSIS"}
            else ("C001" if seed_entity else None)
        )
        try:
            result = E9SemanticOperations(backend, memory, incident).execute(operation, target, 1)
            summary = result["summary"]
            inconclusive = isinstance(summary, dict) and (
                summary.get("available") is False
                or summary.get("assessment") == "INCONCLUSIVE"
                or summary.get("temporal_support") == "INCONCLUSIVE"
            )
            results.append(
                {
                    "operation": operation,
                    "structured": isinstance(summary, (dict, list)),
                    "useful": _useful(operation, summary),
                    "inconclusive": inconclusive,
                    "chars": len(json.dumps(summary, ensure_ascii=False, default=str)),
                    "error": None,
                }
            )
        except (TypeError, ValueError, KeyError) as error:
            results.append(
                {
                    "operation": operation,
                    "structured": False,
                    "useful": False,
                    "inconclusive": False,
                    "chars": 0,
                    "error": str(error)[:200],
                }
            )
    return results


def _useful(operation: str, summary: Any) -> bool:
    if not isinstance(summary, dict):
        return False
    if operation == "RECENT_CHANGE_ANALYSIS":
        return bool(summary.get("change_history_available") and summary.get("changes"))
    if operation == "COMPARE_REPLICAS":
        return bool(summary.get("comparison_available") and summary.get("peer_count", 0) > 0)
    if operation == "TRACE_ERROR_TREE":
        return bool(summary.get("error_tree_available") and summary.get("edges"))
    if operation == "SPEC_ANALYSIS":
        return bool(summary.get("specs") and any(item for item in summary["specs"]))
    if operation == "VERIFY_TEMPORAL_ALIGNMENT":
        return summary.get("temporal_relation") not in {None, "UNKNOWN"}
    if operation == "ENTITY_CONTEXT":
        return bool(summary.get("items") or summary.get("identity") or summary.get("entity"))
    if operation == "INCIDENT_OVERVIEW":
        return bool(summary.get("items") or summary.get("alerts") or summary.get("candidates"))
    return bool(summary.get("items") or summary)


def main() -> int:
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    rows: dict[str, dict[str, Any]] = {
        operation: {
            "operation": operation,
            "scenarios_attempted": 0,
            "successful_structured_result": 0,
            "useful_nonempty_result": 0,
            "unavailable_or_inconclusive": 0,
            "error": 0,
            "output_chars": [],
        }
        for operation in E9_SEMANTIC_OPERATIONS
    }
    scenarios = list(dataset.scenarios())
    with ThreadPoolExecutor(max_workers=8) as executor:
        for results in executor.map(lambda item: _run_scenario(dataset, item), scenarios):
            for result in results:
                row = rows[result["operation"]]
                row["scenarios_attempted"] += 1
                row["successful_structured_result"] += result["structured"]
                row["useful_nonempty_result"] += result["useful"]
                row["unavailable_or_inconclusive"] += result["inconclusive"]
                row["output_chars"].append(result["chars"])
                if result["error"]:
                    row["error"] += 1
                    row.setdefault("errors", []).append(result["error"])
    report_rows = []
    for _operation, row in rows.items():
        values = row.pop("output_chars")
        row["median_output_chars"] = int(median(values)) if values else 0
        row["p95_output_chars"] = (
            sorted(values)[max(0, int(len(values) * 0.95) - 1)] if values else 0
        )
        row["successful_structured_result"] = int(row["successful_structured_result"])
        row["useful_nonempty_result"] = int(row["useful_nonempty_result"])
        row["coverage_status"] = "PASS" if row["error"] == 0 else "ERROR"
        report_rows.append(row)
    payload = {
        "execution": "E9_OFFLINE_PREFLIGHT",
        "purpose": "SEMANTIC_OPERATION_COVERAGE",
        "dataset_scenarios": list(ITBENCH_SCENARIO_IDS),
        "ground_truth_used": False,
        "openai_calls": 0,
        "judge_calls": 0,
        "operations": report_rows,
    }
    atomic_json_write(OUTPUT, payload)
    lines = [
        "# ITBench semantic operation coverage (offline)",
        "",
        "Not an RCA score; no GT or model calls were used.",
        "",
        "| Operation | Scenarios | Structured | Useful | Unavailable/Inconclusive | Errors | Median chars | P95 chars |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report_rows:
        lines.append(
            f"| {row['operation']} | {row['scenarios_attempted']} | {row['successful_structured_result']} | {row['useful_nonempty_result']} | {row['unavailable_or_inconclusive']} | {row['error']} | {row['median_output_chars']} | {row['p95_output_chars']} |"
        )
    MARKDOWN.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "PASS",
                "operations": len(report_rows),
                "scenarios": len(ITBENCH_SCENARIO_IDS),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
