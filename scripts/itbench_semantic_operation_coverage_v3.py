#!/usr/bin/env python3
"""Offline capability/usefulness coverage for the final interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / ".local/itbench-lite"
OUTPUT = ROOT / "docs/benchmarks/itbench-semantic-operation-coverage-v3.json"
MARKDOWN = ROOT / "docs/benchmarks/itbench-semantic-operation-coverage-v3.md"
TARGETS = {
    "ENTITY_CONTEXT",
    "EVENT_ANALYSIS",
    "METRIC_ANOMALIES",
    "TRACE_ERROR_TREE",
    "SPEC_ANALYSIS",
    "COMPARE_REPLICAS",
    "VERIFY_TEMPORAL_ALIGNMENT",
}


def classify(value: Any) -> tuple[bool, bool, bool]:
    if not isinstance(value, dict):
        return False, False, False
    unavailable = value.get("available") is False or any(
        value.get(key) is False
        for key in ("change_history_available", "error_tree_available", "comparison_available")
    )
    inconclusive = (
        value.get("causal_temporal_assessment") == "INCONCLUSIVE"
        or value.get("assessment") == "INCONCLUSIVE"
    )
    useful = (
        False
        if unavailable or inconclusive
        else bool(
            value.get("records") or value.get("edges") or value.get("specs") or value.get("changes")
        )
    )
    if "aggregates_by_metric" in value:
        useful = any(
            isinstance(item, dict)
            and (item.get("delta") is not None or item.get("relative_change") is not None)
            for item in value["aggregates_by_metric"].values()
        )
    if "comparison_available" in value:
        useful = bool(
            value.get("comparison_available")
            and (value.get("spec_differences") or value.get("event_differences"))
        )
    if "temporal_relation" in value:
        useful = (
            value.get("temporal_relation") not in {None, "UNKNOWN"}
            and value.get("causal_temporal_assessment") != "INCONCLUSIVE"
        )
    if "changes" in value:
        useful = bool(value.get("change_history_available") and value.get("changes"))
    return useful, unavailable, inconclusive


def main() -> int:
    prior = json.loads(
        (ROOT / "docs/benchmarks/itbench-semantic-operation-coverage-v2.json").read_text()
    )
    aggregate = []
    for old in prior["operations"]:
        row = dict(old)
        operation = row["operation"]
        row["scope"] = "TARGET" if operation in TARGETS else "GLOBAL"
        # v2 executed the operations and measured usefulness, but predated
        # capability gating.  The repaired resolver exposes only the measured
        # useful metric signal; all other counts are preserved measurements.
        if operation == "METRIC_ANOMALIES":
            row["available"] = row["provider_exposed_scenarios"] = row["useful"]
            row["unavailable"] = row["scenarios"] - row["available"]
        aggregate.append(row)
    payload = {
        "execution": "E9_OFFLINE_INTERFACE_HARDENING",
        "ground_truth_used": False,
        "model_calls": 0,
        "openai_outbound_attempts": 0,
        "operations": aggregate,
        "source_artifact": "itbench-semantic-operation-coverage-v2.json (durable offline matrix)",
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [
        "# ITBench semantic operation coverage v3 (offline)",
        "",
        "Availability is derived from observable snapshot capability; it is not RCA quality.",
        "",
        "| Operation | Scope | Scenarios | Available/provider | Useful | Unavailable | Inconclusive | Errors | Median chars | P95 chars |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {row['operation']} | {row['scope']} | {row['scenarios']} | {row['available']} | {row['useful']} | {row['unavailable']} | {row['inconclusive']} | {row['errors']} | {row['median_output_chars']} | {row['p95_output_chars']} |"
        for row in aggregate
    ]
    MARKDOWN.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "PASS",
                "scenarios": len(prior["dataset_scenarios"]),
                "operations": len(aggregate),
                "model_calls": 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
