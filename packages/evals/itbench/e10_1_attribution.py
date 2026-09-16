"""Post-hoc bottleneck attribution for the immutable E10 prediction trace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchEntity
from packages.evals.itbench.dataset import ITBENCH_SCENARIO_IDS, ITBenchLiteDataset
from packages.evals.itbench.grader import _entity_matches_group

NOT_OBSERVABLE = "NOT_OBSERVABLE_FROM_FROZEN_ARTIFACT"


def build_e10_1_attribution(
    dataset: ITBenchLiteDataset,
    predictions_root: Path,
    local_results_path: Path,
) -> dict[str, Any]:
    """Read frozen E10 traces and compare them only after artifact loading."""
    local_results = json.loads(local_results_path.read_text(encoding="utf-8"))
    result_rows: list[dict[str, Any]] = []
    categories: dict[str, int] = {}
    for scenario_id in ITBENCH_SCENARIO_IDS:
        root_groups = [
            group
            for group in dataset.load_ground_truth(scenario_id).root_cause_groups
            if group.root_cause
        ]
        trial = predictions_root / scenario_id / "1"
        case_state = json.loads((trial / "case_state.json").read_text(encoding="utf-8"))
        trace = json.loads((trial / "turn_trace.json").read_text(encoding="utf-8"))
        discovered = case_state.get("discovered_entities", [])
        canonical_for_handle = {
            item.get("handle"): item.get("canonical")
            for item in discovered
            if isinstance(item, dict)
        }
        root_handles: set[str] = set()
        for handle, canonical in canonical_for_handle.items():
            if isinstance(handle, str) and _matches_root(canonical, root_groups):
                root_handles.add(handle)
        initial_handles: set[str] = set()
        for item in discovered:
            handle = item.get("handle") if isinstance(item, dict) else None
            if isinstance(handle, str) and item.get("discovered_turn") == 0:
                initial_handles.add(handle)
        visible_handles = {
            handle
            for turn in trace
            for handle in turn.get("provider_exposed_targets", [])
            if isinstance(handle, str)
        }
        hypothesized = {
            turn.get("target")
            for turn in trace
            if turn.get("parsed_action") in {"HYPOTHESIZE", "REVISE"}
        }
        investigated = {
            turn.get("target") for turn in trace if turn.get("parsed_action") == "INVESTIGATE"
        }
        candidate_state = case_state.get("candidate_state", {})
        supported = {
            handle
            for handle, value in candidate_state.items()
            if isinstance(value, dict) and value.get("status") == "SUPPORTED"
        }
        submitted = {
            prediction.get("entity")
            for prediction in local_results.get("scenarios", [])
            if prediction.get("scenario_id") == scenario_id
            for prediction in prediction.get("predictions", [])
        }
        root_submitted = any(_matches_root(entity, root_groups) for entity in submitted)
        flags = {
            "root_in_recorded_discovered_entities": bool(root_handles),
            "root_in_initial_recorded_candidates": bool(root_handles & initial_handles),
            "root_ever_discovered": bool(root_handles),
            "root_ever_visible_to_model": bool(root_handles & visible_handles),
            "root_ever_hypothesized": bool(root_handles & hypothesized),
            "root_ever_investigated": bool(root_handles & investigated),
            "root_ever_supported": bool(root_handles & supported),
            "root_submitted": root_submitted,
        }
        category = _primary_category(flags, terminal=_terminal(trace))
        categories[category] = categories.get(category, 0) + 1
        result_rows.append(
            {
                "scenario_id": scenario_id,
                **flags,
                "first_turn": {
                    "discovered": _first_turn(discovered, root_handles, "discovered_turn"),
                    "visible": _first_turn(trace, root_handles, "provider_exposed_targets"),
                    "hypothesized": _first_turn(
                        trace, root_handles, "target", actions={"HYPOTHESIZE", "REVISE"}
                    ),
                    "investigated": _first_turn(
                        trace, root_handles, "target", actions={"INVESTIGATE"}
                    ),
                    "supported": NOT_OBSERVABLE if not supported else None,
                },
                "terminal": _terminal(trace),
                "primary_bottleneck": category,
            }
        )
    total = len(result_rows)
    return {
        "execution": "ITB-E10.1",
        "source_execution": "ITB-E10",
        "predictions_root": str(predictions_root),
        "scenario_count": total,
        "provider_calls": 0,
        "categories": {
            key: {"count": count, "fraction": count / total}
            for key, count in sorted(categories.items())
        },
        "scenarios": result_rows,
    }


def _matches_root(value: Any, groups: list[Any]) -> bool:
    if not isinstance(value, str) or value.count("/") != 2:
        return False
    namespace, kind, name = value.split("/", 2)
    try:
        entity = ITBenchEntity(
            namespace=None if namespace == "_cluster" else namespace,
            kind=kind,
            name=name,
        )
    except ValueError:
        return False
    return any(_entity_matches_group(entity, group) for group in groups)


def _terminal(trace: list[dict[str, Any]]) -> str:
    for item in reversed(trace):
        if item.get("decision") in {"SUBMIT", "STOP", "MODEL_STEP_LIMIT", "PROTOCOL_STALLED"}:
            return str(item["decision"])
    return "UNKNOWN"


def _first_turn(
    values: list[dict[str, Any]],
    root_handles: set[str],
    field: str,
    *,
    actions: set[str] | None = None,
) -> int | str | None:
    for item in values:
        if actions is not None and item.get("parsed_action") not in actions:
            continue
        value = item.get(field)
        if field == "provider_exposed_targets":
            if root_handles.intersection(value or ()):
                return _turn_number(item)
        elif field == "discovered_turn":
            if item.get("handle") in root_handles:
                return int(value) if isinstance(value, int) else NOT_OBSERVABLE
        elif value in root_handles:
            return _turn_number(item)
    return None if root_handles else NOT_OBSERVABLE


def _turn_number(item: dict[str, Any]) -> int:
    value = item.get("turn", 0)
    return value if isinstance(value, int) else 0


def _primary_category(flags: dict[str, bool], *, terminal: str) -> str:
    if not flags["root_in_recorded_discovered_entities"]:
        return "NOT_IN_RECORDED_DISCOVERED_ENTITIES"
    if not flags["root_in_initial_recorded_candidates"]:
        return "INITIAL_RETRIEVAL_MISS"
    if not flags["root_ever_visible_to_model"]:
        return "DYNAMIC_DISCOVERY_MISS"
    if not flags["root_ever_hypothesized"]:
        return "MODEL_SELECTION_MISS"
    if not flags["root_ever_investigated"] or not flags["root_ever_supported"]:
        return "VERIFICATION_MISS"
    if flags["root_submitted"]:
        return "CONCLUSION_MISS"
    if terminal == "STOP":
        return "STOP_WITH_REACHABLE_ROOT"
    return "WRONG_SUBMIT"


__all__ = ["NOT_OBSERVABLE", "build_e10_1_attribution"]
