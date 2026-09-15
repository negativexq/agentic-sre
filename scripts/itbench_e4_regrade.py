#!/usr/bin/env python3
"""Post-hoc regrade of immutable E4 outputs after the generic alias fix."""

from __future__ import annotations

import json
from pathlib import Path

from packages.evals.itbench import (
    ITBenchAgentOutput,
    ITBenchLiteDataset,
    grade_root_cause_entities,
    macro_average,
)
from packages.evals.itbench.grader import ITBenchEntityGrade
from packages.evals.itbench.persistence import atomic_json_write

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "docs/benchmarks/itbench-lite-e4-results.json"
OUT = ROOT / "docs/benchmarks/itbench-e4-regraded.json"


def main() -> None:
    dataset = ITBenchLiteDataset.open(ROOT / ".local/itbench-lite")
    frozen = json.loads(RESULT.read_text(encoding="utf-8"))
    rows = []
    for item in frozen["scenarios"]:
        path = Path(item["checkpoint_path"]) / "outputs/agent_output.json"
        output = ITBenchAgentOutput.model_validate_json(path.read_bytes())
        grade = grade_root_cause_entities(output, dataset.load_ground_truth(item["scenario_id"]))
        old = item["grade"]
        rows.append(
            {
                "scenario": item["scenario_id"],
                "old": {key: old[key] for key in ("true_positive", "precision", "recall", "f1")},
                "corrected": grade.model_dump(mode="json"),
                "changed": old["true_positive"] != grade.true_positive or old["f1"] != grade.f1,
            }
        )
    corrected = [
        ITBenchEntityGrade.model_validate_json(json.dumps(item["corrected"])) for item in rows
    ]
    payload = {
        "artifact_type": "ITBENCH_E4_POSTHOC_REGRADE",
        "identity": "ITB-E4-R",
        "source_result": "docs/benchmarks/itbench-lite-e4-results.json",
        "source_execution_unchanged": True,
        "official_evaluator": "NOT RUN; pinned official evaluator remains secondary",
        "scorer": "corrected deterministic local scorer",
        "macro_entity_metrics": macro_average(corrected),
        "scenarios": rows,
    }
    payload["changed_scenario_count"] = sum(item["changed"] for item in rows)
    payload["entity_hits"] = sum(item["corrected"]["true_positive"] > 0 for item in rows)
    atomic_json_write(OUT, payload)
    print(
        json.dumps(
            {
                "status": "PASS",
                "identity": "ITB-E4-R",
                "changed": payload["changed_scenario_count"],
                "macro": payload["macro_entity_metrics"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
