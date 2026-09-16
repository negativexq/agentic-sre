"""Post-seal deterministic E11 grading boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchAgentOutput
from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.e11_official import E11OfficialManifestV1, verify_e11_seal
from packages.evals.itbench.grader import grade_root_cause_entities, macro_average


def grade_sealed_e11_predictions(
    dataset: ITBenchLiteDataset,
    manifest: E11OfficialManifestV1,
    predictions_root: Path,
    seal_path: Path,
) -> dict[str, Any]:
    """Load GT only after complete seal verification, then grade locally."""
    verify_e11_seal(manifest, predictions_root, seal_path)
    grades = []
    for scenario_id in manifest.scenario_order:
        trial = predictions_root / scenario_id / "1"
        output = ITBenchAgentOutput.model_validate(
            json.loads((trial / "agent_output.json").read_text(encoding="utf-8"))
        )
        grades.append(grade_root_cause_entities(output, dataset.load_ground_truth(scenario_id)))
    macro = macro_average(grades)
    true_positive = sum(item.true_positive for item in grades)
    predicted = sum(item.predicted_count for item in grades)
    ground_truth = sum(item.ground_truth_count for item in grades)
    micro_precision = true_positive / predicted if predicted else 0.0
    micro_recall = true_positive / ground_truth if ground_truth else 0.0
    micro_f1 = (
        2 * micro_precision * micro_recall / (micro_precision + micro_recall)
        if micro_precision + micro_recall
        else 0.0
    )
    return {
        "execution": manifest.execution,
        "scenario_count": len(grades),
        "ground_truth_access": len(grades),
        "provider_invocations": 0,
        "macro": macro,
        "micro": {
            "true_positive": true_positive,
            "predicted": predicted,
            "ground_truth": ground_truth,
            "precision": micro_precision,
            "recall": micro_recall,
            "f1": micro_f1,
        },
        "diagnosis_coverage": sum(item.predicted_count > 0 for item in grades) / len(grades),
        "scenarios": [grade.model_dump(mode="json") for grade in grades],
        "official_judge": "DEFERRED_UNTIL_PREDICTIONS_FROZEN_AND_HUMAN_AUTHORIZED",
    }


__all__ = ["grade_sealed_e11_predictions"]
