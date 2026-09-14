"""Deterministic first-party ITBench-Lite entity metrics.

The official ITBench evaluator remains an optional secondary judge.  This
module never invokes a model and computes only identity matches from the
published ground-truth entity groups and aliases.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.evals.itbench.contracts import (
    ITBenchAgentOutput,
    ITBenchEntity,
    ITBenchGroundTruth,
    ITBenchGroundTruthGroup,
)


class ITBenchEntityGrade(BaseModel):
    """One deterministic entity grade with transparent counts."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str
    predicted_count: int = Field(ge=0)
    ground_truth_count: int = Field(ge=0)
    true_positive: int = Field(ge=0)
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    f1: float = Field(ge=0, le=1)
    predictions: tuple[dict[str, Any], ...] = ()


def grade_root_cause_entities(
    output: ITBenchAgentOutput, ground_truth: ITBenchGroundTruth
) -> ITBenchEntityGrade:
    """Match model entities to GT groups, honoring published aliases."""
    root_groups = [group for group in ground_truth.root_cause_groups if group.root_cause]
    root_ids = {group.group_id for group in root_groups}
    alias_to_root: dict[str, str] = {}
    for group in ground_truth.aliases:
        matching = sorted(root_ids.intersection(group))
        if matching:
            for group_id in group:
                alias_to_root[group_id] = matching[0]
    matched_roots: set[str] = set()
    predictions: list[dict[str, Any]] = []
    for prediction in output.contributing_factor:
        matched = _match_group(prediction.entity, root_groups, alias_to_root, ground_truth)
        if matched is not None:
            matched_roots.add(matched)
        predictions.append(
            {
                "entity": prediction.entity.canonical,
                "matches_gt": matched is not None,
                "matched_to": matched,
            }
        )
    predicted_count = len(output.contributing_factor)
    gt_count = len(root_groups)
    true_positive = len(matched_roots)
    precision = true_positive / predicted_count if predicted_count else 0.0
    recall = true_positive / gt_count if gt_count else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ITBenchEntityGrade(
        scenario_id=output.scenario_id,
        predicted_count=predicted_count,
        ground_truth_count=gt_count,
        true_positive=true_positive,
        precision=precision,
        recall=recall,
        f1=f1,
        predictions=tuple(predictions),
    )


def _match_group(
    entity: ITBenchEntity,
    groups: list[ITBenchGroundTruthGroup],
    aliases: dict[str, str],
    gt: ITBenchGroundTruth,
) -> str | None:
    """Return the root group ID matching an entity, or ``None``."""
    for group in groups:
        if _entity_matches_group(entity, group):
            return group.group_id
        for alias_group_id, root_id in aliases.items():
            if alias_group_id != group.group_id:
                continue
            alias_group = next(
                (item for item in gt.root_cause_groups if item.group_id == alias_group_id), None
            )
            if alias_group is not None and _entity_matches_group(entity, alias_group):
                return root_id
    return None


def _entity_matches_group(entity: ITBenchEntity, group: ITBenchGroundTruthGroup) -> bool:
    if entity.kind.casefold() != group.kind.casefold():
        return False
    if group.namespace and entity.namespace != group.namespace:
        return False
    concrete = group.entity()
    if concrete is not None and concrete.name == entity.name:
        return True
    for expression in group.filters:
        try:
            if re.search(expression, entity.name):
                return True
        except re.error:
            if expression == entity.name:
                return True
    return False


def macro_average(grades: list[ITBenchEntityGrade]) -> dict[str, float | int]:
    """Return deterministic macro means and raw scenario denominator."""
    if not grades:
        return {"scenario_count": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0}
    count = len(grades)
    return {
        "scenario_count": count,
        "precision": sum(item.precision for item in grades) / count,
        "recall": sum(item.recall for item in grades) / count,
        "f1": sum(item.f1 for item in grades) / count,
    }


__all__ = ["ITBenchEntityGrade", "grade_root_cause_entities", "macro_average"]
