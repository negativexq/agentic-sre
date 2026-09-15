#!/usr/bin/env python3
"""Generate a ground-truth-free forensic summary of the frozen E4 run."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "docs/benchmarks/itbench-lite-e4-results.json"
OUT_JSON = ROOT / "docs/benchmarks/itbench-e4-failure-attribution.json"
OUT_MD = ROOT / "docs/benchmarks/itbench-e4-failure-attribution.md"


def main() -> None:
    frozen = json.loads(RESULT.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    classes: Counter[str] = Counter()
    for scenario in frozen["scenarios"]:
        checkpoint = Path(scenario["checkpoint_path"]) / "native_artifact.json"
        artifact = json.loads(checkpoint.read_text(encoding="utf-8"))
        usage = artifact.get("usage", scenario.get("usage", {}))
        terminal = scenario["terminal"]
        validation = {
            "stage": usage.get("validation_stage"),
            "path": usage.get("validation_path"),
            "type": usage.get("validation_type"),
        }
        if terminal == "TOOL_CALL_LIMIT":
            primary = "A_TOOL_BUDGET_EXHAUSTION"
        elif terminal == "MODEL_DECISION_INVALID":
            primary = (
                "C_EVIDENCE_REFERENCE_FAILURE"
                if validation["stage"] == "EVIDENCE_REFERENCE"
                else "B_MODEL_DECISION_SCHEMA_FAILURE"
            )
        elif terminal == "STOP":
            primary = "D_VOLUNTARY_STOP"
        elif terminal == "SUBMIT_DIAGNOSIS":
            primary = (
                "F_DIAGNOSIS_PARTIAL_CORRECTNESS"
                if scenario["grade"]["true_positive"]
                else "E_DIAGNOSIS_WRONG_CAUSE"
            )
        else:
            primary = "G_OTHER"
        classes[primary] += 1
        turns = artifact.get("turns", [])
        requested = [name for turn in turns for name in turn.get("requested_tools", [])]
        duplicates = sum(
            1
            for turn in turns
            for item in turn.get("summaries", [])
            if item.get("status") == "SKIPPED_DUPLICATE"
        )
        failures = sum(
            1
            for turn in turns
            for item in turn.get("summaries", [])
            if item.get("status") == "TOOL_EXECUTION_FAILURE"
        )
        rows.append(
            {
                "scenario": scenario["scenario_id"],
                "primary_failure_class": primary,
                "terminal": terminal,
                "model_calls": usage.get("model_calls", 0),
                "outbound_api_attempts": usage.get("outbound_api_attempts", 0),
                "tool_requests": usage.get("tool_requests_total", len(requested)),
                "tool_executions": usage.get("tool_calls", 0),
                "duplicate_reused": duplicates,
                "typed_tool_failures": failures,
                "evidence_count": scenario.get("evidence_count", len(artifact.get("evidence", []))),
                "input_tokens": usage.get("input_tokens_total", 0),
                "output_tokens": usage.get("output_tokens_total", 0),
                "validation": validation,
                "predicted_entities": scenario.get("predicted_entities", 0),
                "true_positive": scenario["grade"].get("true_positive", 0),
                "tool_names": sorted(set(requested)),
            }
        )
    payload = {
        "artifact_type": "ITBENCH_E4_FAILURE_ATTRIBUTION",
        "source_execution": frozen["execution"],
        "source_result_sha256": frozen.get("result_head"),
        "ground_truth_used_for": "existing frozen grade fields only; no behavior changes",
        "scenario_count": len(rows),
        "primary_failure_class_counts": dict(sorted(classes.items())),
        "scenarios": rows,
        "interpretation": {
            "scoring": "alias-aware scorer requires post-hoc audit; never changes investigation",
            "tool_semantics": "entity_context/entity lookup and typed aggregates are audited independently",
            "policy": "tool budget and duplicate request behavior are diagnostic, not score filters",
            "model": "residual wrong entities after valid terminal remain model/causal outcomes",
        },
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# ITB-E4 failure attribution",
        "",
        "This is a post-hoc forensic report over the immutable E4 checkpoints. It does not alter or reinterpret E4.",
        "",
        "| Primary class | Scenarios |",
        "|---|---:|",
    ]
    labels = {
        "A_TOOL_BUDGET_EXHAUSTION": "Tool-budget exhaustion",
        "B_MODEL_DECISION_SCHEMA_FAILURE": "Decision/schema failure",
        "C_EVIDENCE_REFERENCE_FAILURE": "Evidence reference failure",
        "D_VOLUNTARY_STOP": "Voluntary STOP",
        "E_DIAGNOSIS_WRONG_CAUSE": "Diagnosis submitted, wrong cause",
        "F_DIAGNOSIS_PARTIAL_CORRECTNESS": "Diagnosis submitted, partial correctness",
        "G_OTHER": "Other",
    }
    lines.extend(f"| {labels.get(key, key)} | {value} |" for key, value in sorted(classes.items()))
    lines += [
        "",
        "| Scenario | Terminal | Primary class | Requests | Executions | Duplicates | Typed failures | Validation |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        validation_label = row["validation"]["type"] or "—"
        lines.append(
            f"| {row['scenario']} | {row['terminal']} | {labels.get(row['primary_failure_class'], row['primary_failure_class'])} | {row['tool_requests']} | {row['tool_executions']} | {row['duplicate_reused']} | {row['typed_tool_failures']} | {validation_label} |"
        )
    lines += [
        "",
        "## Attribution boundaries",
        "",
        "Tool-budget, contract, and evidence-interface defects are system findings. A valid but wrong submitted entity remains a model/causal outcome; no scenario-specific repair is derived from ground truth.",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
