"""Post-hoc, ground-truth-aware forensic report for the frozen ITB-E6 run.

The report is deliberately separate from investigator code.  Ground truth is
loaded only after the persisted prediction and observable trace have been
read, and the resulting classifications are never consumed by runtime code.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchAgentOutput
from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.grader import grade_root_cause_entities

DATASET = Path(".local/itbench-lite")
RUNS = Path(".local/itbench-lite-e6-runs/official")
OUT_JSON = Path("docs/benchmarks/itbench-e6-rca-forensics.json")
OUT_MD = Path("docs/benchmarks/itbench-e6-rca-forensics.md")


def _entities(output: ITBenchAgentOutput) -> list[str]:
    return [item.entity.canonical for item in output.contributing_factor]


def _group_match(entity: str, group: Any) -> bool:
    parts = entity.split("/", 2)
    if len(parts) != 3 or parts[1].casefold() != group.kind.casefold():
        return False
    if group.namespace and parts[0] != group.namespace:
        return False
    if group.name and parts[2] == group.name:
        return True
    import re

    return any(re.search(expression, parts[2]) for expression in group.filters)


def _category(predicted: str, gt: Any, topology: list[dict[str, Any]]) -> str:
    roots = [group for group in gt.root_cause_groups if group.root_cause]
    if any(_group_match(predicted, group) for group in roots):
        return "EXACT_OR_ROOT_FILTER_MATCH"
    aliases = {value for group in gt.aliases for value in group}
    if predicted in aliases:
        return "ALIAS_OF_ROOT"
    pred_parts = predicted.split("/", 2)
    if len(pred_parts) != 3:
        return "UNRELATED_ENTITY"
    neighbors = {edge["source"] for edge in topology if edge.get("target") == predicted} | {
        edge["target"] for edge in topology if edge.get("source") == predicted
    }
    root_canonicals = {
        f"{group.namespace or '_cluster'}/{group.kind}/{group.name}"
        for group in roots
        if group.name
    }
    if neighbors & root_canonicals:
        return "DIRECT_PARENT_OR_CHILD_OF_ROOT"
    if pred_parts[0] in {group.namespace for group in roots if group.namespace}:
        if pred_parts[2].split("-")[0] in {str(group.name).split("-")[0] for group in roots}:
            return "SAME_WORKLOAD_FAMILY"
    if pred_parts[1] in {group.kind for group in roots}:
        return "WRONG_ENTITY_NORMALIZATION"
    return "UNRELATED_ENTITY"


def main() -> None:
    dataset = ITBenchLiteDataset.open(DATASET)
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for trial in sorted(RUNS.glob("*/1")):
        native_path = trial / "native_artifact.json"
        output_path = trial / "outputs" / "agent_output.json"
        if not native_path.exists() or not output_path.exists():
            continue
        native = json.loads(native_path.read_text(encoding="utf-8"))
        output = ITBenchAgentOutput.model_validate_json(output_path.read_text(encoding="utf-8"))
        scenario_id = output.scenario_id
        # Observable trajectory is read before the post-hoc GT load.
        trajectory = native.get("turns", [])
        predicted = _entities(output)
        gt = dataset.load_ground_truth(scenario_id)
        grade = grade_root_cause_entities(output, gt)
        classifications = [_category(entity, gt, []) for entity in predicted]
        primary = (
            "VOLUNTARY_STOP"
            if native.get("terminal") == "STOP"
            else "MODEL_DECISION_INVALID"
            if native.get("terminal") == "MODEL_DECISION_INVALID"
            else "DIAGNOSIS_WRONG_CAUSE"
            if grade.true_positive == 0
            else "DIAGNOSIS_PARTIAL_OR_CORRECT"
        )
        counts[primary] += 1
        rows.append(
            {
                "scenario": scenario_id,
                "terminal": native.get("terminal"),
                "model_calls": native.get("usage", {}).get("model_calls"),
                "tool_requests": native.get("usage", {}).get("tool_requests_total"),
                "tool_executions": native.get("usage", {}).get("tool_calls"),
                "tool_names": [
                    name for turn in trajectory for name in turn.get("requested_tools", [])
                ],
                "duplicate_or_reused": sum(
                    1
                    for turn in trajectory
                    for item in turn.get("summaries", [])
                    if item.get("status") == "SKIPPED_DUPLICATE"
                ),
                "typed_failures": sum(
                    1
                    for turn in trajectory
                    for item in turn.get("summaries", [])
                    if item.get("status") in {"TOOL_EXECUTION_FAILURE", "INVALID_TOOL_REQUEST"}
                ),
                "evidence_count": len(native.get("evidence", [])),
                "validation_stage": native.get("usage", {}).get("validation_stage"),
                "validation_path": native.get("usage", {}).get("validation_path"),
                "validation_type": native.get("usage", {}).get("validation_type"),
                "input_tokens": native.get("usage", {}).get("input_tokens_total", 0),
                "output_tokens": native.get("usage", {}).get("output_tokens_total", 0),
                "predicted_entities": predicted,
                "prediction_categories": classifications,
                "ground_truth_root_groups": [
                    {
                        "id": group.group_id,
                        "kind": group.kind,
                        "namespace": group.namespace,
                        "name": group.name,
                        "filters": list(group.filters),
                    }
                    for group in gt.root_cause_groups
                    if group.root_cause
                ],
                "true_positive": grade.true_positive,
                "precision": grade.precision,
                "recall": grade.recall,
                "f1": grade.f1,
                "primary_failure_class": primary,
            }
        )
    payload = {
        "execution": "ITB-E6",
        "source": "persisted E6 predictions and traces; GT used post-hoc only",
        "scenario_count": len(rows),
        "primary_failure_counts": dict(sorted(counts.items())),
        "scenarios": rows,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# ITB-E6 RCA forensic audit",
        "",
        "Ground truth is used only after reading frozen prediction and trajectory artifacts.",
        "The classifications below are post-hoc diagnostics and are not runtime behavior.",
        "",
        "## Primary failure classes",
        "",
        "| class | scenarios | percent |",
        "|---|---:|---:|",
    ]
    for key, value in sorted(counts.items()):
        lines.append(
            f"| {key} | {value} | {value / len(rows):.2%} |"
            if rows
            else f"| {key} | {value} | 0% |"
        )
    lines += [
        "",
        "## Prediction-level details",
        "",
        "| scenario | terminal | predicted | TP | P | R | F1 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['scenario']} | {row['terminal']} | {', '.join(row['predicted_entities']) or '∅'} | "
            f"{row['true_positive']} | {row['precision']:.3f} | {row['recall']:.3f} | {row['f1']:.3f} |"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
