#!/usr/bin/env python3
"""Build a bounded forensic report from the immutable E5 checkpoints."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

RESULT = Path("docs/benchmarks/itbench-lite-e5-results.json")
ROOT = Path(".local/itbench-lite-e5-runs/official")
JSON_OUT = Path("docs/benchmarks/itbench-e5-forensic-audit.json")
MD_OUT = Path("docs/benchmarks/itbench-e5-forensic-audit.md")


def main() -> None:
    result = json.loads(RESULT.read_text(encoding="utf-8"))
    scenarios: list[dict[str, Any]] = []
    classes: Counter[str] = Counter()
    secondary: Counter[str] = Counter()
    for summary in result["scenarios"]:
        scenario_id = str(summary["scenario_id"])
        artifact = json.loads(
            (ROOT / scenario_id / "1/native_artifact.json").read_text(encoding="utf-8")
        )
        usage = artifact.get("usage", {})
        turns = artifact.get("turns", [])
        names = [
            str(name)
            for turn in turns
            for name in turn.get("requested_tools", [])
            if isinstance(name, str)
        ]
        statuses = [
            str(item.get("status"))
            for turn in turns
            for item in turn.get("summaries", [])
            if isinstance(item, dict) and item.get("status")
        ]
        validation_stage = usage.get("validation_stage")
        validation_path = usage.get("validation_path")
        validation_type = usage.get("validation_type")
        terminal = str(summary["terminal"])
        if terminal == "MODEL_DECISION_INVALID" and validation_path == "$.requests":
            primary = "WIRE_LOCAL_SCHEMA_MISMATCH"
            secondary["request cardinality / local maxItems"] += 1
        elif terminal == "MODEL_DECISION_INVALID":
            primary = "EARLY_INVALID_DECISION"
        elif terminal == "STOP":
            primary = "STOP"
        elif terminal == "SUBMIT_DIAGNOSIS" and int(summary.get("true_positive", 0)) > 0:
            primary = "PARTIAL_OR_CORRECT_DIAGNOSIS"
        elif terminal == "SUBMIT_DIAGNOSIS":
            primary = "WRONG_DIAGNOSIS"
        else:
            primary = terminal
        if "TOOL_EXECUTION_FAILURE" in statuses:
            secondary["typed tool failure"] += 1
        if any(
            item.get("error_code") == "TOOL_TIMEOUT"
            for turn in turns
            for item in turn.get("summaries", [])
            if isinstance(item, dict)
        ):
            secondary["metric/tool timeout"] += 1
        if "SKIPPED_DUPLICATE" in statuses:
            secondary["duplicate/reused request"] += 1
        classes[primary] += 1
        scenarios.append(
            {
                "scenario": scenario_id,
                "terminal": terminal,
                "primary_failure_class": primary,
                "model_calls": usage.get("model_calls", summary.get("model_calls")),
                "provider_attempts": usage.get(
                    "outbound_api_attempts", summary.get("provider_outbound_delta")
                ),
                "tool_requests": usage.get("tool_requests_total", len(names)),
                "tool_executions": usage.get("tool_calls", summary.get("tool_calls")),
                "tool_names": names,
                "tool_statuses": Counter(statuses),
                "duplicate_reused": statuses.count("SKIPPED_DUPLICATE"),
                "typed_failures": statuses.count("TOOL_EXECUTION_FAILURE"),
                "evidence_count": len(artifact.get("evidence", [])),
                "predicted_entities": summary.get("predicted_entities", 0),
                "validation_stage": validation_stage,
                "validation_path": validation_path,
                "validation_type": validation_type,
                "input_tokens": usage.get("input_tokens_total", usage.get("input_tokens", 0)),
                "output_tokens": usage.get("output_tokens_total", usage.get("output_tokens", 0)),
                "duration_ms": usage.get("duration_ms", summary.get("duration_ms")),
            }
        )
    report = {
        "identity": {
            "execution": "ITB-E5",
            "experiment": "itbench-lite-sre-external-eval-v5",
            "source": str(RESULT),
            "immutable_checkpoint_root": str(ROOT),
        },
        "scenario_count": len(scenarios),
        "primary_failure_counts": dict(classes),
        "secondary_observations": dict(secondary),
        "scenarios": scenarios,
        "provenance": "derived only from persisted E5 result/checkpoint metadata; no ground truth loaded",
    }
    JSON_OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# ITBench E5 forensic audit",
        "",
        "Derived from immutable E5 result/checkpoint metadata. Ground truth was not loaded.",
        "",
        "## Primary failure classes",
        "",
        "| Class | Scenarios | Percentage |",
        "|---|---:|---:|",
    ]
    total = len(scenarios)
    for key, value in classes.items():
        lines.append(
            f"| {key} | {value} | {value / total:.2%} |" if total else f"| {key} | {value} | 0% |"
        )
    lines += ["", "## Secondary observations", ""]
    for key, value in secondary.items():
        lines.append(f"- {key}: {value}")
    lines += [
        "",
        "## Interpretation",
        "",
        "- `MODEL_DECISION_INVALID` at `$.requests` is an adapter/local-schema mismatch, not an RCA score.",
        "- Tool timeout and typed-failure counts are runtime/tool observations; they do not imply ground-truth access.",
        "- Wrong diagnoses and STOP remain model outcomes.",
        "",
    ]
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps({"status": "PASS", "scenarios": total, "primary": dict(classes)}, sort_keys=True)
    )


if __name__ == "__main__":
    main()
