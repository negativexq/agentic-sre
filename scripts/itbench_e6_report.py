#!/usr/bin/env python3
"""Assemble post-run E4/E5/E6 comparison from durable result artifacts."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, cast

E4 = Path("docs/benchmarks/itbench-lite-e4-results.json")
E5 = Path("docs/benchmarks/itbench-lite-e5-results.json")
E6 = Path("docs/benchmarks/itbench-lite-e6-results.json")
E4_ROOT = Path(".local/itbench-lite-e4-runs")
E5_ROOT = Path(".local/itbench-lite-e5-runs/official")
E6_ROOT = Path(".local/itbench-lite-e6-runs/official")
OUT_JSON = Path("docs/benchmarks/itbench-e4-e5-e6-comparison.json")
OUT_MD = Path("docs/benchmarks/itbench-e4-e5-e6-comparison.md")


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def per_scenario(path: Path, root: Path) -> list[dict[str, Any]]:
    result = load(path)
    rows: list[dict[str, Any]] = []
    for item in result["scenarios"]:
        scenario = str(item.get("scenario_id"))
        artifact_path = root / scenario / "1/native_artifact.json"
        artifact = load(artifact_path) if artifact_path.exists() else {}
        usage = artifact.get("usage", item.get("usage", {}))
        turns = artifact.get("turns", [])
        statuses = [
            str(summary.get("status"))
            for turn in turns
            for summary in turn.get("summaries", [])
            if isinstance(summary, dict) and summary.get("status")
        ]
        request_count = int(usage.get("tool_requests_total", item.get("tool_requests", 0)) or 0)
        tool_count = int(
            usage.get("tool_calls", item.get("tool_calls", item.get("backend_operations", 0))) or 0
        )
        rows.append(
            {
                "scenario": scenario,
                "terminal": item.get("terminal", artifact.get("terminal")),
                "model_calls": int(usage.get("model_calls", item.get("model_calls", 0)) or 0),
                "provider_attempts": int(
                    usage.get("outbound_api_attempts", item.get("provider_outbound_delta", 0)) or 0
                ),
                "tool_requests": request_count,
                "tool_executions": tool_count,
                "semantic_actions": sum(bool(turn.get("requested_tools")) for turn in turns),
                "evidence_count": len(artifact.get("evidence", []))
                or int(item.get("evidence_count", 0) or 0),
                "duplicates": statuses.count("SKIPPED_DUPLICATE"),
                "typed_failures": statuses.count("TOOL_EXECUTION_FAILURE"),
                "timeouts": sum(
                    1
                    for turn in turns
                    for summary in turn.get("summaries", [])
                    if isinstance(summary, dict) and summary.get("error_code") == "TOOL_TIMEOUT"
                ),
                "predicted_entities": int(item.get("predicted_entities", 0) or 0),
                "true_positive": int(
                    item.get("true_positive", item.get("grade", {}).get("true_positive", 0)) or 0
                ),
                "precision": float(
                    item.get("precision", item.get("grade", {}).get("precision", 0.0)) or 0.0
                ),
                "recall": float(
                    item.get("recall", item.get("grade", {}).get("recall", 0.0)) or 0.0
                ),
                "f1": float(item.get("f1", item.get("grade", {}).get("f1", 0.0)) or 0.0),
                "duration_ms": int(usage.get("duration_ms", item.get("duration_ms", 0)) or 0),
                "input_tokens": int(
                    usage.get("input_tokens_total", usage.get("input_tokens", 0)) or 0
                ),
                "output_tokens": int(
                    usage.get("output_tokens_total", usage.get("output_tokens", 0)) or 0
                ),
            }
        )
    return rows


def aggregate(rows: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    terminals = Counter(str(row["terminal"]) for row in rows)
    return {
        "scenario_count": len(rows),
        "macro": result.get("macro", result.get("macro_entity_metrics", {})),
        "entity_hits": sum(row["true_positive"] > 0 for row in rows),
        "diagnosis_submissions": sum(row["terminal"] == "SUBMIT_DIAGNOSIS" for row in rows),
        "terminal_distribution": dict(terminals),
        "provider_calls": sum(row["provider_attempts"] for row in rows),
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "tool_requests": sum(row["tool_requests"] for row in rows),
        "tool_executions": sum(row["tool_executions"] for row in rows),
        "semantic_actions": sum(row["semantic_actions"] for row in rows),
        "evidence_producing_operations": sum(row["evidence_count"] for row in rows),
        "duplicates": sum(row["duplicates"] for row in rows),
        "typed_failures": sum(row["typed_failures"] for row in rows),
        "timeouts": sum(row["timeouts"] for row in rows),
        "mean_duration_ms": statistics.mean(row["duration_ms"] for row in rows) if rows else 0,
        "conditional": {
            "p_diagnosis": sum(row["terminal"] == "SUBMIT_DIAGNOSIS" for row in rows) / len(rows)
            if rows
            else 0,
            "p_correct_given_diagnosis": sum(
                row["true_positive"] > 0 for row in rows if row["terminal"] == "SUBMIT_DIAGNOSIS"
            )
            / sum(row["terminal"] == "SUBMIT_DIAGNOSIS" for row in rows)
            if any(row["terminal"] == "SUBMIT_DIAGNOSIS" for row in rows)
            else 0,
            "p_correct_overall": sum(row["true_positive"] > 0 for row in rows) / len(rows)
            if rows
            else 0,
        },
    }


def main() -> None:
    sources = {"E4": (E4, E4_ROOT), "E5": (E5, E5_ROOT), "E6": (E6, E6_ROOT)}
    all_runs: dict[str, Any] = {}
    for name, (path, root) in sources.items():
        result = load(path)
        rows = per_scenario(path, root)
        all_runs[name] = {"aggregate": aggregate(rows, result), "scenarios": rows}
    all_runs["E6"]["official_judge"] = "NOT RUN"
    comparison = {
        "purpose": "post-run engineering comparison from durable checkpoints",
        "benchmark_validity": {
            "E4": "first valid frozen external baseline",
            "E5": "engineering iteration confounded by request-contract and metric-tool defects",
            "E6": "post-E5 engineering iteration on the same public benchmark; not untouched held-out generalization",
        },
        "runs": all_runs,
        "official_evaluator": {
            "revision": "14f026fc9cc348c4ecec5ab32714de954c95c1b1",
            "status": "NOT RUN / unavailable in local checkout",
        },
    }
    OUT_JSON.write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metrics = [
        ("Macro F1", "macro.f1"),
        ("Entity hits / 35", "entity_hits"),
        ("Diagnosis coverage", "diagnosis_submissions"),
        ("MODEL_DECISION_INVALID", "terminal_distribution.MODEL_DECISION_INVALID"),
        ("TOOL_CALL_LIMIT", "terminal_distribution.TOOL_CALL_LIMIT"),
        ("STOP", "terminal_distribution.STOP"),
        ("Tool requests", "tool_requests"),
        ("Tool executions", "tool_executions"),
        ("Duplicate actions", "duplicates"),
        ("Input tokens", "input_tokens"),
    ]
    lines = [
        "# ITBench E4 → E5 → E6 comparison",
        "",
        "Derived from durable result/checkpoint artifacts; official judge not run.",
        "",
        "| Metric | E4 | E5 | E6 |",
        "|---|---:|---:|---:|",
    ]
    for label, key in metrics:
        values = []
        for name in ("E4", "E5", "E6"):
            value: Any = all_runs[name]["aggregate"]
            for part in key.split("."):
                value = value.get(part, 0) if isinstance(value, dict) else 0
            values.append(value)
        lines.append(f"| {label} | {values[0]} | {values[1]} | {values[2]} |")
    lines += ["", "## Conditional diagnosis metrics", ""]
    for name in ("E4", "E5", "E6"):
        c = all_runs[name]["aggregate"]["conditional"]
        lines.append(
            f"- {name}: P(diagnosis)={c['p_diagnosis']:.5f}; P(correct | diagnosis)={c['p_correct_given_diagnosis']:.5f}; P(correct overall)={c['p_correct_overall']:.5f}"
        )
    lines += [
        "",
        "## Validity note",
        "",
        "E5 and E6 are engineering iterations on the same public scenarios after the E4 baseline; neither is untouched held-out generalization.",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {name: value["aggregate"] for name, value in all_runs.items()}, indent=2, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
