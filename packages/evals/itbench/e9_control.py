"""One deterministic control surface shared by context, provider and runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.evals.itbench.e9_semantic import E9_SEMANTIC_OPERATIONS


@dataclass(frozen=True, slots=True)
class E9ControlSurface:
    """The provider-visible and runtime-accepted action surface for one state."""

    phase: str
    actions: tuple[str, ...]
    operations: tuple[str, ...]
    target_handles: tuple[str, ...]
    final_turn: bool
    rejection_budget_remaining: int


def control_surface(
    state: dict[str, Any],
    *,
    turn: int,
    max_steps: int,
    max_rejections: int,
    semantic_limit: int,
) -> E9ControlSurface:
    """Derive all capabilities from the materialized CaseState only."""
    phase = str(state.get("current_phase", "OBSERVE"))
    final_turn = turn >= max_steps
    has_hypothesis = state.get("current_hypothesis") is not None
    has_evidence = bool(state.get("evidence"))
    if final_turn:
        actions: tuple[str, ...] = (
            ("SUBMIT", "STOP") if has_hypothesis and has_evidence else ("STOP",)
        )
        operations: tuple[str, ...] = ()
    elif phase == "OBSERVE":
        actions = ("OBSERVE", "HYPOTHESIZE", "STOP")
        operations = (
            "INCIDENT_OVERVIEW",
            "ALERT_ANALYSIS",
            "TOPOLOGY_ANALYSIS",
            "RECENT_CHANGE_ANALYSIS",
            "EVENT_ANALYSIS",
        )
    elif phase == "VERIFY":
        actions = ("OBSERVE", "INVESTIGATE", "REVISE", "SUBMIT", "STOP")
        operations = (
            "ENTITY_CONTEXT",
            "METRIC_ANOMALIES",
            "TRACE_ERROR_TREE",
            "SPEC_ANALYSIS",
            "COMPARE_REPLICAS",
            "VERIFY_TEMPORAL_ALIGNMENT",
        )
    else:
        actions = ("OBSERVE", "HYPOTHESIZE", "INVESTIGATE", "REVISE", "STOP")
        operations = E9_SEMANTIC_OPERATIONS

    completed = {
        (item.get("entity_handle"), item.get("operation"))
        for item in state.get("operations_already_run", [])
    }
    target = state.get("current_hypothesis")
    current_target = target.get("entity_handle") if isinstance(target, dict) else None
    target_handles: tuple[str, ...]
    if current_target is not None:
        target_handles = (str(current_target),)
    else:
        target_handles = tuple(
            str(item.get("handle"))
            for item in state.get("discovered_entities", {}).values()
            if isinstance(item, dict) and isinstance(item.get("handle"), str)
        )
    available_operations: tuple[str, ...] = tuple(
        operation
        for operation in operations
        if (current_target, operation) not in completed
        and int(state.get("semantic_actions_used", 0)) < semantic_limit
    )
    return E9ControlSurface(
        phase=phase,
        actions=actions,
        operations=available_operations,
        target_handles=target_handles,
        final_turn=final_turn,
        rejection_budget_remaining=max(
            max_rejections - int(state.get("consecutive_rejections", 0)), 0
        ),
    )


__all__ = ["E9ControlSurface", "control_surface"]
