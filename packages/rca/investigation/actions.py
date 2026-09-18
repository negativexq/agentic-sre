"""Deterministic validation for model-selected investigation actions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from packages.rca.investigation.state import InvestigationConfig, InvestigationTool
from packages.rca.model import GapResolvability, InformationGap, InvestigationAction


@dataclass(frozen=True)
class ActionValidation:
    """Result of the policy gate."""

    valid: bool
    reason: str = ""
    gap: InformationGap | None = None
    tool: InvestigationTool | None = None


def action_identity(action: InvestigationAction) -> str:
    """Stable identity used to prevent repeated no-op observations."""
    target = action.target.canonical if action.target is not None else "-"
    return f"{action.gap_id or '-'}|{action.capability or '-'}|{target}"


def validate_action(
    action: InvestigationAction,
    *,
    gaps: Sequence[InformationGap],
    tools: Mapping[str, InvestigationTool],
    attempted_actions: Sequence[str],
    tool_calls: int,
    config: InvestigationConfig,
) -> ActionValidation:
    """Fail closed on every action property before a tool can execute."""
    if action.action == "stop":
        if action.gap_id is not None or action.capability is not None or action.target is not None:
            return ActionValidation(False, "stop actions cannot contain an inspection target")
        return ActionValidation(True)

    if action.gap_id is None or action.capability is None or action.target is None:
        return ActionValidation(False, "inspect requires gap_id, capability, and target")
    gap = next((item for item in gaps if item.gap_id == action.gap_id), None)
    if gap is None:
        return ActionValidation(False, f"unknown information gap {action.gap_id!r}")
    if gap.resolvability is not GapResolvability.RESOLVABLE:
        return ActionValidation(False, f"gap is not resolvable: {gap.resolvability.value}", gap=gap)
    tool = tools.get(action.capability)
    if tool is None:
        return ActionValidation(False, f"unsupported capability {action.capability!r}", gap=gap)
    if action.capability not in gap.candidate_tools:
        return ActionValidation(
            False,
            f"capability {action.capability!r} is not allowed for gap {gap.gap_id}",
            gap=gap,
            tool=tool,
        )
    allowed = set(gap.entity_scope)
    if action.target not in allowed:
        return ActionValidation(
            False,
            f"target {action.target.canonical!r} is outside the gap entity scope",
            gap=gap,
            tool=tool,
        )
    identity = action_identity(action)
    if identity in attempted_actions:
        return ActionValidation(False, "the same action was already attempted", gap=gap, tool=tool)
    if tool_calls >= config.max_tool_calls:
        return ActionValidation(False, "tool-call budget exhausted", gap=gap, tool=tool)
    gap_attempts = sum(item.startswith(f"{gap.gap_id}|") for item in attempted_actions)
    if gap_attempts >= config.max_tool_calls_per_gap:
        return ActionValidation(False, "per-gap tool-call budget exhausted", gap=gap, tool=tool)
    return ActionValidation(True, gap=gap, tool=tool)


__all__ = ["ActionValidation", "action_identity", "validate_action"]
