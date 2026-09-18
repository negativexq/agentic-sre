"""Deterministic validation for model-selected investigation actions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from packages.rca.investigation.state import InvestigationConfig, InvestigationTool
from packages.rca.model import (
    EntityRef,
    GapResolvability,
    InformationGap,
    InvestigationAction,
    InvestigationQuery,
)


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
    query = (
        json.dumps(action.query.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        if action.query is not None
        else "-"
    )
    return f"{action.gap_id or '-'}|{action.capability or '-'}|{target}|{query}"


def observation_identity(
    capability: str,
    target: EntityRef,
    query: InvestigationQuery | None,
) -> str:
    """Identity of the underlying backend read, independent of gap identity."""
    if capability in {"describe", "neighbors"} or query is None:
        effective: dict[str, object] = {}
    elif capability == "history":
        effective = {"start": query.start, "end": query.end, "limit": query.limit}
    elif capability == "events":
        effective = {
            "start": query.start,
            "end": query.end,
            "reasons": tuple(sorted(set(query.reasons))),
            "contains": tuple(sorted(set(query.contains))),
            "limit": query.limit,
        }
    elif capability == "logs":
        effective = {
            "start": query.start,
            "end": query.end,
            "contains": tuple(sorted(set(query.contains))),
            "limit": query.limit,
        }
    else:
        effective = {"start": query.start, "end": query.end, "limit": query.limit}
    encoded = json.dumps(effective, default=str, sort_keys=True, separators=(",", ":"))
    return f"{capability}|{target.canonical}|{encoded}"


def validate_action(
    action: InvestigationAction,
    *,
    gaps: Sequence[InformationGap],
    tools: Mapping[str, InvestigationTool],
    attempted_actions: Sequence[str],
    tool_calls: int,
    config: InvestigationConfig,
    attempted_observations: Sequence[str] = (),
) -> ActionValidation:
    """Fail closed on every action property before a tool can execute."""
    if action.action == "stop":
        if (
            action.gap_id is not None
            or action.capability is not None
            or action.target is not None
            or action.query is not None
        ):
            return ActionValidation(False, "stop actions cannot contain an inspection target")
        return ActionValidation(True)

    if action.gap_id is None or action.capability is None or action.target is None:
        return ActionValidation(False, "inspect requires gap_id, capability, and target")
    if action.query is not None and action.query.start and action.query.end:
        if action.query.start > action.query.end:
            return ActionValidation(False, "query start must not be after query end")
    gap = next((item for item in gaps if item.gap_id == action.gap_id), None)
    if gap is None:
        return ActionValidation(False, f"unknown information gap {action.gap_id!r}")
    if gap.resolvability is not GapResolvability.RESOLVABLE:
        return ActionValidation(False, f"gap is not resolvable: {gap.resolvability.value}", gap=gap)
    tool = tools.get(action.capability)
    if tool is None:
        return ActionValidation(False, f"unsupported capability {action.capability!r}", gap=gap)
    authorization = next(
        (
            item
            for item in gap.authorized_queries
            if item.capability == action.capability and item.target == action.target
        ),
        None,
    )
    if authorization is None:
        return ActionValidation(
            False,
            "capability/target pair is not authorized for this gap",
            gap=gap,
            tool=tool,
        )
    identity = action_identity(action)
    if identity in attempted_actions:
        return ActionValidation(False, "the same action was already attempted", gap=gap, tool=tool)
    read_identity = observation_identity(action.capability, action.target, action.query)
    if read_identity in attempted_observations:
        return ActionValidation(
            False,
            "the same telemetry observation was already attempted",
            gap=gap,
            tool=tool,
        )
    if tool_calls >= config.max_tool_calls:
        return ActionValidation(False, "tool-call budget exhausted", gap=gap, tool=tool)
    gap_attempts = sum(item.startswith(f"{gap.gap_id}|") for item in attempted_actions)
    if gap_attempts >= config.max_tool_calls_per_gap:
        return ActionValidation(False, "per-gap tool-call budget exhausted", gap=gap, tool=tool)
    return ActionValidation(True, gap=gap, tool=tool)


__all__ = [
    "ActionValidation",
    "action_identity",
    "observation_identity",
    "validate_action",
]
