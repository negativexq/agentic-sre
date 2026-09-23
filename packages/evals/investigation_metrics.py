"""Versioned, deterministic metrics derived from persisted investigation audits.

M14 V1 definitions use only runtime artifacts. This module does not accept
scenario labels, ground truth, or grader output.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from packages.rca.model import (
    HypothesisEpistemicState,
    InvestigationActionAudit,
    InvestigationExecutionStatus,
    InvestigationResult,
    Resolution,
)

METRIC_VERSION: Literal["m14.v1"] = "m14.v1"


class InvestigationMetricsV1(BaseModel):
    """One investigation's runtime efficiency, impact, outcome and safety."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_version: Literal["m14.v1"] = METRIC_VERSION
    model_calls: int
    tool_calls: int
    unique_observations: int
    useful_call_count: int
    useful_call_rate: float | None
    novel_evidence_count: int
    novel_evidence_rate: float | None
    duplicate_read_count: int
    duplicate_read_rate: float | None
    no_data_count: int
    no_data_rate: float | None
    decision_relevant_call_count: int
    decision_relevant_call_rate: float | None
    hypotheses_changed: int
    alternatives_eliminated: int
    gap_state_changes: int
    resolution_transitions: int
    resolved_during_investigation: bool
    reads_to_resolution: int | None
    invalid_actions: int
    rejected_actions: int
    out_of_policy_execution: int
    write_execution: int
    secret_access: int


def _executed(audit: InvestigationActionAudit) -> bool:
    return audit.backend_execution_status is not InvestigationExecutionStatus.NOT_EXECUTED


def _hypothesis_snapshot(audit: InvestigationActionAudit) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (item.hypothesis_id, item.actor.canonical, item.state.value)
            for item in audit.hypothesis_states_before
        )
    )


def _hypothesis_snapshot_after(
    audit: InvestigationActionAudit,
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (item.hypothesis_id, item.actor.canonical, item.state.value)
            for item in audit.hypothesis_states_after
        )
    )


def _gap_snapshot(audit: InvestigationActionAudit) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (item.gap_id, item.dimension.value, item.resolvability.value)
            for item in audit.gap_states_before
        )
    )


def _gap_snapshot_after(audit: InvestigationActionAudit) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (item.gap_id, item.dimension.value, item.resolvability.value)
            for item in audit.gap_states_after
        )
    )


def _semantic_read_identity(audit: InvestigationActionAudit) -> str:
    query = audit.action.query
    query_value = query.model_dump(mode="json", exclude_none=True) if query else {}
    return json.dumps(
        {
            "capability": audit.action.capability,
            "target": audit.action.target.canonical if audit.action.target else None,
            "query": query_value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_useful(audit: InvestigationActionAudit) -> bool:
    if audit.normalized_finding_ids:
        return True
    if _hypothesis_snapshot(audit) != _hypothesis_snapshot_after(audit):
        return True
    if _gap_snapshot(audit) != _gap_snapshot_after(audit):
        return True
    if audit.resolution_after is not None and audit.resolution_before != audit.resolution_after:
        return True
    if audit.leading_actor_before != audit.leading_actor_after:
        return True
    # FRONTIER_PROGRESS records newly covered deterministic structural-query
    # opportunities without claiming a causal finding.
    return audit.progress_classification == "FRONTIER_PROGRESS"


def _is_decision_relevant(audit: InvestigationActionAudit) -> bool:
    return (
        _hypothesis_snapshot(audit) != _hypothesis_snapshot_after(audit)
        or _gap_snapshot(audit) != _gap_snapshot_after(audit)
        or audit.resolution_before != audit.resolution_after
        or audit.leading_actor_before != audit.leading_actor_after
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _forbidden_execution_count(audits: Iterable[InvestigationActionAudit], *, secret: bool) -> int:
    total = 0
    for audit in audits:
        if not _executed(audit):
            continue
        capability = (audit.action.capability or "").casefold()
        target_kind = audit.action.target.kind.casefold() if audit.action.target else ""
        if secret:
            violation = "secret" in capability or target_kind == "secret"
        else:
            mutation_tokens = ("write", "apply", "patch", "delete", "create", "scale")
            violation = any(token in capability for token in mutation_tokens)
        total += int(violation)
    return total


def derive_investigation_metrics(result: InvestigationResult) -> InvestigationMetricsV1:
    """Derive M14 V1 metrics without consulting evaluation labels.

    A useful call changes deterministic findings, hypothesis/gap/resolution or
    leading-actor state, or makes recorded structural frontier progress. A
    decision-relevant call changes hypothesis, gap, resolution, or actor state;
    novel evidence alone is not decision relevance. A duplicate read is a
    repeated semantic capability/target/query in one run, or a non-empty read
    whose returned refs are all already-known. Rates use executed backend
    attempts as denominator and are undefined (``None``) when there are none.
    """
    audits = result.action_audits
    executed = tuple(item for item in audits if _executed(item))
    tool_calls = len(executed)
    if tool_calls != result.tool_calls:
        raise ValueError(
            "persisted executed-action count does not match InvestigationResult.tool_calls"
        )

    seen_reads: set[str] = set()
    duplicate_count = 0
    novel_refs: set[str] = set()
    novel_calls = 0
    for audit in executed:
        identity = _semantic_read_identity(audit)
        returned = set(audit.returned_evidence_refs)
        new = set(audit.new_evidence_refs)
        known = set(audit.already_known_refs)
        novel_refs.update(new)
        novel_calls += int(bool(new))
        repeated_identity = identity in seen_reads
        all_evidence_known = bool(returned) and not new and returned <= known
        duplicate_count += int(repeated_identity or all_evidence_known)
        seen_reads.add(identity)

    useful_count = sum(_is_useful(item) for item in executed)
    decision_count = sum(_is_decision_relevant(item) for item in executed)
    hypothesis_changes = sum(
        _hypothesis_snapshot(item) != _hypothesis_snapshot_after(item) for item in executed
    )
    eliminated_ids = {
        after.hypothesis_id
        for item in executed
        for after in item.hypothesis_states_after
        if after.state is HypothesisEpistemicState.CONTRADICTED
        and all(
            before.hypothesis_id != after.hypothesis_id
            or before.state is not HypothesisEpistemicState.CONTRADICTED
            for before in item.hypothesis_states_before
        )
    }
    gap_changes = sum(_gap_snapshot(item) != _gap_snapshot_after(item) for item in executed)
    resolution_transitions = sum(
        item.resolution_after is not None and item.resolution_before != item.resolution_after
        for item in executed
    )
    no_data_count = sum(
        item.observation_outcome is not None and item.observation_outcome.value == "NO_DATA"
        for item in executed
    )
    reads_to_resolution: int | None = None
    if result.initial_resolution is Resolution.RESOLVED:
        reads_to_resolution = 0
    elif result.final_resolution is Resolution.RESOLVED:
        resolution_turns = [
            item.turn_index for item in executed if item.resolution_after is Resolution.RESOLVED
        ]
        if resolution_turns:
            reads_to_resolution = min(resolution_turns)

    denominator = tool_calls
    return InvestigationMetricsV1(
        model_calls=result.model_calls,
        tool_calls=tool_calls,
        unique_observations=len({item.observation_id for item in result.observations}),
        useful_call_count=useful_count,
        useful_call_rate=_rate(useful_count, denominator),
        novel_evidence_count=len(novel_refs),
        novel_evidence_rate=_rate(novel_calls, denominator),
        duplicate_read_count=duplicate_count,
        duplicate_read_rate=_rate(duplicate_count, denominator),
        no_data_count=no_data_count,
        no_data_rate=_rate(no_data_count, denominator),
        decision_relevant_call_count=decision_count,
        decision_relevant_call_rate=_rate(decision_count, denominator),
        hypotheses_changed=hypothesis_changes,
        alternatives_eliminated=len(eliminated_ids),
        gap_state_changes=gap_changes,
        resolution_transitions=resolution_transitions,
        resolved_during_investigation=result.resolved_during_investigation,
        reads_to_resolution=reads_to_resolution,
        invalid_actions=sum(item.authorization_result == "REJECTED" for item in audits),
        rejected_actions=result.rejected_actions,
        out_of_policy_execution=sum(
            item.authorization_result != "AUTHORIZED"
            and item.backend_execution_status is not InvestigationExecutionStatus.NOT_EXECUTED
            for item in audits
        ),
        write_execution=_forbidden_execution_count(audits, secret=False),
        secret_access=_forbidden_execution_count(audits, secret=True),
    )
