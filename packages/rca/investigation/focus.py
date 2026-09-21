"""Deterministic causal-focus projections for physical observation selection."""

from __future__ import annotations

from collections.abc import Sequence

from packages.rca.engine import Case
from packages.rca.investigation.candidates import ObservationCandidate
from packages.rca.investigation.intents import InvestigationIntentKind
from packages.rca.investigation.selection import (
    candidate_to_action,
    rank_observation_candidates,
)
from packages.rca.model import Diagnosis, EntityRef, InvestigationLedgerEntry
from packages.rca.root_cause_eligibility import RootCauseEligibilityState
from packages.rca.topology import WORKLOAD_KINDS


def _normalized_workload(case: Case, entity: EntityRef) -> EntityRef | None:
    if entity.kind in WORKLOAD_KINDS:
        return entity
    return case.topology.workload_of(entity)


def _active_anchor_workloads(case: Case, diagnosis: Diagnosis) -> frozenset[EntityRef]:
    trace = diagnosis.resolution_trace
    if trace is None:
        return frozenset()
    hypothesis_ids = set(trace.leading_hypothesis_ids)
    hypothesis_ids.update(trace.unresolved_hypotheses)
    if not trace.unresolved_hypotheses:
        hypothesis_ids.update(trace.plausible_hypotheses)
    hypotheses = {item.hypothesis_id: item for item in case.hypotheses}
    workloads: set[EntityRef] = set()
    for hypothesis_id in hypothesis_ids:
        hypothesis = hypotheses.get(hypothesis_id)
        if hypothesis is None:
            continue
        eligibility = case.root_cause_eligibilities.for_hypothesis(hypothesis_id)
        if (
            eligibility is not None
            and eligibility.state is RootCauseEligibilityState.INELIGIBLE_PROPAGATED_EFFECT
        ):
            continue
        workload = _normalized_workload(case, hypothesis.causal_actor)
        if workload is not None:
            workloads.add(workload)
    return frozenset(workloads)


def exact_workload_dependency_candidates(
    *,
    intent: InvestigationIntentKind,
    candidates: Sequence[ObservationCandidate],
    case: Case,
    diagnosis: Diagnosis,
    attempted_observations: Sequence[str] = (),
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
    max_tool_calls_per_gap: int | None = None,
) -> tuple[ObservationCandidate, ...]:
    """Return executable dependency-log candidates on active causal workloads.

    This is a prefilter only.  It does not rank, execute, or mutate candidates;
    the existing physical selector remains responsible for selection within the
    returned region.
    """
    if intent is not InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION:
        return ()
    anchor_workloads = _active_anchor_workloads(case, diagnosis)
    if not anchor_workloads:
        return ()
    focused: list[ObservationCandidate] = []
    for candidate in candidates:
        if candidate.capability != "logs":
            continue
        workload = _normalized_workload(case, candidate.target)
        if workload is None or workload not in anchor_workloads:
            continue
        ranked = rank_observation_candidates(
            candidates=(candidate,),
            diagnosis=diagnosis,
            attempted_observations=attempted_observations,
            previous_investigations=previous_investigations,
        )
        if not ranked:
            continue
        if (
            candidate_to_action(
                ranked[0],
                diagnosis,
                previous_investigations=previous_investigations,
                max_tool_calls_per_gap=max_tool_calls_per_gap,
            )
            is None
        ):
            continue
        focused.append(candidate)
    return tuple(focused)


__all__ = ["exact_workload_dependency_candidates"]
