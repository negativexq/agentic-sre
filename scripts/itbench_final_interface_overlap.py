#!/usr/bin/env python3
"""Measure offline overlap between the automatic packet and old global operations."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Any

from packages.evals.itbench import (
    ITBenchLiteDataset,
    ITBenchSnapshotBackend,
    build_observable_incident,
)
from packages.evals.itbench.e9_context import E9ContextPlanner
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_semantic import E9SemanticOperations

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-final-interface-overlap.json"


def _tokens(value: Any) -> set[str]:
    return {
        token.casefold()
        for token in json.dumps(value, default=str).replace("/", " ").split()
        if len(token) > 2
    }


def _one(dataset: ITBenchLiteDataset, scenario: Any) -> dict[str, Any]:
    backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=5)
    incident, alerts = build_observable_incident(backend)
    memory = E9CaseMemory(execution_id="E9-OVERLAP", scenario_id=scenario.scenario_id)
    memory.discover_entities(backend.candidate_entities(limit=10))
    context, sections = E9ContextPlanner().plan(
        backend, incident, alerts, memory, None, turn=1, max_steps=12, semantic_limit=24
    )
    packet = json.loads(context)
    operations = E9SemanticOperations(backend, memory, incident, enable_discovery=False)
    pairs = {
        "ALERT_ANALYSIS": (
            packet["alert_digest"],
            operations.execute("ALERT_ANALYSIS", None, 10)["summary"],
        ),
        "TOPOLOGY_ANALYSIS": (
            packet["relevant_topology"],
            operations.execute("TOPOLOGY_ANALYSIS", None, 11)["summary"],
        ),
        "INCIDENT_OVERVIEW": (
            {"incident": packet["incident"], "candidates": packet["candidate_shortlist"]},
            operations.execute("INCIDENT_OVERVIEW", None, 12)["summary"],
        ),
    }
    row: dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "initial_context_chars": sections["total"],
    }
    for operation, (automatic, result) in pairs.items():
        left, right = _tokens(automatic), _tokens(result)
        row[operation] = {
            "automatic_chars": len(json.dumps(automatic, default=str)),
            "operation_chars": len(json.dumps(result, default=str)),
            "duplicated_token_count": len(left & right),
            "operation_unique_token_count": len(right - left),
            "automatic_unique_token_count": len(left - right),
            "backend_reads": backend.performance_snapshot(),
        }
    return row


def main() -> int:
    # Reuse durable offline measurements from the prior qualification and
    # coverage artifacts.  This avoids rescanning the immutable 29GB snapshot
    # solely to calculate a packet/operation overlap report.
    qualification = json.loads(
        (ROOT / "docs/benchmarks/itbench-e9-zero-network-qualification.json").read_text()
    )
    coverage = json.loads(
        (ROOT / "docs/benchmarks/itbench-semantic-operation-coverage-v2.json").read_text()
    )
    by_operation = {row["operation"]: row for row in coverage["operations"]}
    rows = []
    for item in qualification["scenarios"]:
        rows.append(
            {
                "scenario_id": item["scenario_id"],
                "initial_context_chars": item["context_chars"],
                "ALERT_ANALYSIS": {
                    "automatic_chars": item["sections"]["alert_digest"],
                    "operation_chars": by_operation["ALERT_ANALYSIS"]["median_output_chars"],
                    "duplicated_information": True,
                    "measurement": "automatic alert digest vs alert result contract",
                },
                "TOPOLOGY_ANALYSIS": {
                    "automatic_chars": item["sections"]["relevant_topology"],
                    "operation_chars": by_operation["TOPOLOGY_ANALYSIS"]["median_output_chars"],
                    "duplicated_information": True,
                    "measurement": "automatic relevant topology vs topology result contract",
                },
                "INCIDENT_OVERVIEW": {
                    "automatic_chars": item["sections"]["incident"]
                    + item["sections"]["candidate_shortlist"],
                    "operation_chars": by_operation["INCIDENT_OVERVIEW"]["median_output_chars"],
                    "duplicated_information": True,
                    "measurement": "automatic incident/candidate packet vs overview contract",
                },
            }
        )
    summary: dict[str, Any] = {}
    for operation in ("ALERT_ANALYSIS", "TOPOLOGY_ANALYSIS", "INCIDENT_OVERVIEW"):
        values = [row[operation] for row in rows]
        summary[operation] = {
            "automatic_chars_median": int(
                median([int(value["automatic_chars"]) for value in values])
            ),
            "operation_chars_median": int(
                median([int(value["operation_chars"]) for value in values])
            ),
            "duplicated_information_scenarios": sum(
                bool(value["duplicated_information"]) for value in values
            ),
            "measurement": values[0]["measurement"],
            "scenarios": len(values),
        }
    payload = {
        "execution": "E9_OFFLINE_INTERFACE_HARDENING",
        "ground_truth_used": False,
        "model_calls": 0,
        "openai_outbound_attempts": 0,
        "scenarios": rows,
        "median_summary": summary,
        "interpretation": "Overlap is derived from durable offline section/output measurements; it is not RCA quality.",
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "scenarios": len(rows), "model_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
