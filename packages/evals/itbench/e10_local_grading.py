"""Provider-free local grading, reachable only after E10 prediction sealing."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchAgentOutput
from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.e10_official import (
    E10OfficialManifestV1,
    E10PredictionError,
    verify_e10_seal,
)
from packages.evals.itbench.grader import (
    ITBenchEntityGrade,
    grade_root_cause_entities,
    macro_average,
)
from packages.evals.itbench.persistence import atomic_json_write


def _micro(grades: list[ITBenchEntityGrade]) -> dict[str, Any]:
    predicted = sum(item.predicted_count for item in grades)
    truth = sum(item.ground_truth_count for item in grades)
    true_positive = sum(item.true_positive for item in grades)
    precision = true_positive / predicted if predicted else 0.0
    recall = true_positive / truth if truth else 0.0
    return {
        "true_positive": true_positive,
        "predicted": predicted,
        "ground_truth": truth,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
    }


def grade_local_e10(
    root: Path,
    *,
    manifest: E10OfficialManifestV1,
    manifest_path: Path,
    predictions_root: Path,
    seal_path: Path,
    result_path: Path,
) -> dict[str, Any]:
    """Verify the immutable seal, then perform deterministic local grading."""
    verify_e10_seal(
        root, manifest_path=manifest_path, predictions_root=predictions_root, seal_path=seal_path
    )
    dataset = ITBenchLiteDataset.open(root / ".local/itbench-lite")
    grades: list[ITBenchEntityGrade] = []
    scenarios: list[dict[str, Any]] = []
    for scenario_id in manifest.scenario_order:
        output_path = predictions_root / scenario_id / "1" / "agent_output.json"
        output = ITBenchAgentOutput.model_validate_json(output_path.read_text(encoding="utf-8"))
        grade = grade_root_cause_entities(output, dataset.load_ground_truth(scenario_id))
        grades.append(grade)
        scenarios.append(grade.model_dump(mode="json"))
    if len(grades) != manifest.scenario_count:
        raise E10PredictionError("local grading did not cover all sealed scenarios")
    payload = {
        "execution": manifest.execution,
        "experiment": manifest.experiment,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "prediction_seal_sha256": hashlib.sha256(seal_path.read_bytes()).hexdigest(),
        "scenario_count": len(grades),
        "scenario_order": list(manifest.scenario_order),
        "scenarios": scenarios,
        "local_fixed_35_macro": macro_average(grades),
        "local_fixed_35_micro": _micro(grades),
        "diagnosis_coverage": sum(
            item.native_terminal == "SUBMIT_DIAGNOSIS"
            for item in (
                ITBenchAgentOutput.model_validate_json(
                    (predictions_root / scenario_id / "1" / "agent_output.json").read_text()
                )
                for scenario_id in manifest.scenario_order
            )
        )
        / manifest.scenario_count,
        "official_judge": "DEFERRED_UNTIL_PREDICTIONS_FROZEN_AND_HUMAN_AUTHORIZED",
        "provider_calls": 0,
    }
    atomic_json_write(result_path, payload)
    return payload


__all__ = ["grade_local_e10"]
