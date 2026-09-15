"""One deterministic control surface shared by context, provider and runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from packages.evals.itbench.e9_semantic import E9_SEMANTIC_OPERATION_SPECS, E9_SEMANTIC_OPERATIONS


@dataclass(frozen=True, slots=True)
class E9ControlSurface:
    """The provider-visible and runtime-accepted action surface for one state."""

    phase: str
    actions: tuple[str, ...]
    operations: tuple[str, ...]
    target_handles: tuple[str, ...]
    final_turn: bool
    rejection_budget_remaining: int

    @property
    def all_actions(self) -> tuple[str, ...]:
        return self.actions


@dataclass(frozen=True, slots=True)
class E9ControlPlaneVariant:
    """Explicit switches used by the offline control-plane ablation."""

    recoverable_rejections: bool = True
    visible_rejection_feedback: bool = True
    dynamic_action_gating: bool = True
    dynamic_operation_gating: bool = True
    semantic_facade: bool = True
    selective_context: bool = True
    dynamic_candidate_discovery: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {
            "recoverable_rejections": self.recoverable_rejections,
            "visible_rejection_feedback": self.visible_rejection_feedback,
            "dynamic_action_gating": self.dynamic_action_gating,
            "dynamic_operation_gating": self.dynamic_operation_gating,
            "semantic_facade": self.semantic_facade,
            "selective_context": self.selective_context,
            "dynamic_candidate_discovery": self.dynamic_candidate_discovery,
        }

    def config_hash(self) -> str:
        encoded = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()


def control_surface(
    state: dict[str, Any],
    *,
    turn: int,
    max_steps: int,
    max_rejections: int,
    semantic_limit: int,
    available_operations: tuple[str, ...] | None = None,
    target_handles: tuple[str, ...] | None = None,
    **_: Any,
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
    discovered = [
        item
        for item in state.get("discovered_entities", {}).values()
        if isinstance(item, dict) and isinstance(item.get("handle"), str)
    ]
    discovered.sort(
        key=lambda item: (
            item.get("handle") != current_target,
            -int(item.get("discovered_turn", 0)),
            str(item.get("handle")),
        )
    )
    resolved_target_handles = target_handles or tuple(
        str(item["handle"]) for item in discovered[:12]
    )
    resolved_operations: tuple[str, ...] = tuple(
        operation
        for operation in operations
        if (
            (None, operation)
            if next(spec for spec in E9_SEMANTIC_OPERATION_SPECS if spec.name == operation).scope
            == "GLOBAL"
            else (current_target, operation)
        )
        not in completed
        and int(state.get("semantic_actions_used", 0)) < semantic_limit
    )
    return E9ControlSurface(
        phase=phase,
        actions=actions,
        operations=available_operations
        if available_operations is not None
        else resolved_operations,
        target_handles=resolved_target_handles,
        final_turn=final_turn,
        rejection_budget_remaining=max(
            max_rejections - int(state.get("consecutive_rejections", 0)), 0
        ),
    )


__all__ = ["E9ControlPlaneVariant", "E9ControlSurface", "control_surface"]
