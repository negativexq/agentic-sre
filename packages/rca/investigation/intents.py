"""Deterministic semantic investigation intents over physical observations.

This module is deliberately a planning layer.  It never reads telemetry and it
does not create authority: every member of a bundle is an already-authorized
physical observation candidate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from packages.rca.engine import Case, EngineConfig
from packages.rca.investigation.candidates import (
    ObservationCandidate,
    build_observation_candidates,
)
from packages.rca.investigation.selection import (
    ScoredObservationCandidate,
    candidate_to_action,
    is_observation_candidate_admissible,
    rank_observation_candidates,
)
from packages.rca.model import (
    Diagnosis,
    GapDimension,
    GapResolvability,
    InvestigationAction,
    InvestigationLedgerEntry,
)
from packages.rca.root_cause_eligibility import (
    RootCauseEligibilityState,
    episode_source_capable_initiating_findings,
)


class InvestigationIntentKind(StrEnum):
    INCIDENT_ACTOR_DISCOVERY = "INCIDENT_ACTOR_DISCOVERY"
    RECENT_SOURCE_CHANGE = "RECENT_SOURCE_CHANGE"
    DEPENDENCY_ERROR_INSPECTION = "DEPENDENCY_ERROR_INSPECTION"
    ACTOR_STATE_INSPECTION = "ACTOR_STATE_INSPECTION"
    METRIC_STATE_INSPECTION = "METRIC_STATE_INSPECTION"
    RUNTIME_DISCRIMINATION = "RUNTIME_DISCRIMINATION"


class InvestigationPhase(StrEnum):
    SOURCE_DISCOVERY = "SOURCE_DISCOVERY"
    HYPOTHESIS_DISCRIMINATION = "HYPOTHESIS_DISCRIMINATION"
    FOCUSED_FOLLOWUP = "FOCUSED_FOLLOWUP"


@dataclass(frozen=True)
class ObservationBundle:
    """One semantic question backed by already-authorized physical reads."""

    bundle_id: str
    intent: InvestigationIntentKind
    phase: InvestigationPhase
    candidate_ids: tuple[str, ...]
    capabilities: tuple[str, ...]
    gap_ids: tuple[str, ...]
    dimensions: tuple[GapDimension, ...]
    hypothesis_ids: tuple[str, ...]
    alternative_ids: tuple[str, ...]


@dataclass(frozen=True)
class IntentUtility:
    """Auditable ordinal utility; no field is a probability or weighted score."""

    admissible: bool
    phase_match: int
    decision_blocker_match: int
    leading_hypothesis_relevance: int
    unresolved_hypothesis_relevance: int
    eligibility_relevance: int
    semantic_priority: int
    stable_tiebreak: str


@dataclass(frozen=True)
class ScoredObservationBundle:
    bundle: ObservationBundle
    utility: IntentUtility


@dataclass(frozen=True)
class SelectedObservationIntent:
    """The selected semantic bundle and the physical read chosen within it."""

    phase: InvestigationPhase
    scored_bundle: ScoredObservationBundle
    physical: ScoredObservationCandidate


_CAPABILITY_INTENT = {
    "incident_events": InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY,
    "incident_changes": InvestigationIntentKind.RECENT_SOURCE_CHANGE,
    "logs": InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION,
    "runtime_traces": InvestigationIntentKind.RUNTIME_DISCRIMINATION,
    "resource_pressure": InvestigationIntentKind.METRIC_STATE_INSPECTION,
    "traffic": InvestigationIntentKind.METRIC_STATE_INSPECTION,
    "events": InvestigationIntentKind.ACTOR_STATE_INSPECTION,
}

_SOURCE_ORDER = {
    InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY: 5,
    InvestigationIntentKind.RECENT_SOURCE_CHANGE: 4,
    InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION: 3,
    InvestigationIntentKind.ACTOR_STATE_INSPECTION: 2,
    InvestigationIntentKind.METRIC_STATE_INSPECTION: 1,
    InvestigationIntentKind.RUNTIME_DISCRIMINATION: 0,
}
_DISCRIMINATION_ORDER = {
    InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION: 6,
    InvestigationIntentKind.ACTOR_STATE_INSPECTION: 5,
    InvestigationIntentKind.RECENT_SOURCE_CHANGE: 4,
    InvestigationIntentKind.RUNTIME_DISCRIMINATION: 3,
    InvestigationIntentKind.METRIC_STATE_INSPECTION: 2,
    InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY: 1,
}
_CHANGE_DIMENSIONS = frozenset({GapDimension.CONFIG_DIFFERENCE, GapDimension.CHANGE_TIMING})
_SOURCE_DIMENSIONS = frozenset(
    {
        GapDimension.EVENT_SEQUENCE,
        GapDimension.FAILURE_ONSET,
        GapDimension.CONFIG_DIFFERENCE,
        GapDimension.CHANGE_TIMING,
    }
)
_LOG_DIMENSIONS = frozenset({GapDimension.LOG_ERROR_PATTERN, GapDimension.DEPENDENCY_HEALTH})
_METRIC_DIMENSIONS = frozenset(
    {GapDimension.METRIC_BASELINE, GapDimension.METRIC_CHANGE, GapDimension.RESOURCE_PRESSURE}
)


def classify_observation_candidate(
    candidate: ObservationCandidate,
) -> InvestigationIntentKind:
    """Classify one physical candidate exactly once from bounded metadata."""
    if candidate.capability == "history":
        return (
            InvestigationIntentKind.RECENT_SOURCE_CHANGE
            if set(candidate.dimensions) & _CHANGE_DIMENSIONS
            else InvestigationIntentKind.ACTOR_STATE_INSPECTION
        )
    try:
        return _CAPABILITY_INTENT[candidate.capability]
    except KeyError as error:
        raise ValueError(
            f"unsupported observation capability requires explicit intent mapping: "
            f"{candidate.capability}"
        ) from error


def _hypothesis_sets(diagnosis: Diagnosis) -> tuple[set[str], set[str], set[str]]:
    trace = diagnosis.resolution_trace
    if trace is None:
        return (
            set(),
            set(),
            {
                hypothesis.hypothesis_id
                for hypothesis in (
                    *diagnosis.ambiguous_hypotheses,
                    *diagnosis.alternative_hypotheses,
                )
            },
        )
    leading = set(trace.leading_hypothesis_ids)
    unresolved = set(trace.unresolved_hypotheses)
    plausible = set(trace.plausible_hypotheses)
    if not unresolved:
        unresolved = set(plausible)
    return leading, unresolved, plausible


def _has_source_hypothesis(case: Case, diagnosis: Diagnosis) -> bool:
    trace = diagnosis.resolution_trace
    if trace is not None:
        visible_ids = (
            set(trace.leading_hypothesis_ids)
            | set(trace.unresolved_hypotheses)
            | set(trace.plausible_hypotheses)
        )
        if not visible_ids:
            return False
        hypotheses = tuple(
            hypothesis for hypothesis in case.hypotheses if hypothesis.hypothesis_id in visible_ids
        )
    else:
        hypotheses = tuple(case.hypotheses)
    return any(
        bool(episode_source_capable_initiating_findings(hypothesis)) for hypothesis in hypotheses
    )


def _has_discrimination_state(case: Case, diagnosis: Diagnosis) -> bool:
    trace = diagnosis.resolution_trace
    leading, unresolved, plausible = _hypothesis_sets(diagnosis)
    relevant = leading | unresolved | plausible
    if len(relevant) >= 2:
        return True
    if trace is not None and trace.unresolved_dimensions:
        return True
    for hypothesis_id in relevant:
        eligibility = case.root_cause_eligibilities.for_hypothesis(hypothesis_id)
        if eligibility is not None and eligibility.state is RootCauseEligibilityState.UNDETERMINED:
            return True
    return False


def derive_investigation_phase(case: Case, diagnosis: Diagnosis) -> InvestigationPhase:
    """Derive phase from the current deterministic state; never persist it."""
    if not _has_source_hypothesis(case, diagnosis):
        return InvestigationPhase.SOURCE_DISCOVERY
    if _has_discrimination_state(case, diagnosis):
        return InvestigationPhase.HYPOTHESIS_DISCRIMINATION
    return InvestigationPhase.FOCUSED_FOLLOWUP


def _runtime_relevant(
    candidate: ObservationCandidate,
    diagnosis: Diagnosis,
) -> bool:
    leading, unresolved, plausible = _hypothesis_sets(diagnosis)
    return bool(set(candidate.hypothesis_ids) & (leading | unresolved | plausible))


def _intent_allowed(
    intent: InvestigationIntentKind,
    phase: InvestigationPhase,
    candidates: Sequence[ObservationCandidate],
    diagnosis: Diagnosis,
) -> bool:
    if intent is not InvestigationIntentKind.RUNTIME_DISCRIMINATION:
        return True
    return (
        phase is InvestigationPhase.HYPOTHESIS_DISCRIMINATION
        and bool(
            diagnosis.hypothesis
            or diagnosis.ambiguous_hypotheses
            or diagnosis.alternative_hypotheses
        )
        and any(_runtime_relevant(candidate, diagnosis) for candidate in candidates)
    )


def _blocker_match(
    bundle: ObservationBundle,
    diagnosis: Diagnosis,
    phase: InvestigationPhase,
    source_actor_bundle_available: bool = True,
) -> int:
    current = {
        gap.dimension
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE
        and (not gap.hypothesis_ids or set(gap.hypothesis_ids) & set(bundle.hypothesis_ids))
    }
    dimensions = set(bundle.dimensions)
    overlap = dimensions & current
    if overlap:
        if phase is InvestigationPhase.SOURCE_DISCOVERY:
            if (
                not source_actor_bundle_available
                and bundle.intent is InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION
                and overlap & _LOG_DIMENSIONS
            ):
                return 4
            return (
                2
                if bundle.intent
                in {
                    InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY,
                    InvestigationIntentKind.RECENT_SOURCE_CHANGE,
                }
                else 0
            )
        if (
            bundle.intent is InvestigationIntentKind.DEPENDENCY_ERROR_INSPECTION
            and overlap & _LOG_DIMENSIONS
        ):
            return 4
        if (
            bundle.intent is InvestigationIntentKind.RECENT_SOURCE_CHANGE
            and overlap & _CHANGE_DIMENSIONS
        ):
            return 4
        if bundle.intent is InvestigationIntentKind.RUNTIME_DISCRIMINATION and overlap & {
            GapDimension.DEPENDENCY_HEALTH,
            GapDimension.TOPOLOGY_RELATION,
        }:
            return 4
        return 3
    if (
        bundle.intent is InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY
        and dimensions & _SOURCE_DIMENSIONS
    ):
        return 2
    if (
        bundle.intent is InvestigationIntentKind.RECENT_SOURCE_CHANGE
        and dimensions & _CHANGE_DIMENSIONS
    ):
        return 2
    return 0


def _hypothesis_relevance(bundle: ObservationBundle, diagnosis: Diagnosis) -> tuple[int, int]:
    leading, unresolved, _plausible = _hypothesis_sets(diagnosis)
    bundle_ids = set(bundle.hypothesis_ids)
    return int(bool(bundle_ids & leading)), int(bool(bundle_ids & unresolved))


def build_observation_bundles(
    *,
    case: Case,
    diagnosis: Diagnosis,
    candidates: Sequence[ObservationCandidate],
    attempted_observations: Sequence[str] = (),
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
) -> tuple[ObservationBundle, ...]:
    """Group every currently admissible physical candidate into one intent."""
    phase = derive_investigation_phase(case, diagnosis)
    grouped: dict[InvestigationIntentKind, list[ObservationCandidate]] = {}
    for candidate in candidates:
        if not is_observation_candidate_admissible(
            candidate,
            attempted_observations=attempted_observations,
            previous_investigations=previous_investigations,
        ):
            continue
        intent = classify_observation_candidate(candidate)
        if intent is InvestigationIntentKind.RUNTIME_DISCRIMINATION and not _intent_allowed(
            intent, phase, (candidate,), diagnosis
        ):
            continue
        grouped.setdefault(intent, []).append(candidate)

    bundles: list[ObservationBundle] = []
    for intent, members in grouped.items():
        ordered = tuple(sorted(members, key=lambda item: item.candidate_id))
        bundles.append(
            ObservationBundle(
                bundle_id=f"intent:{intent.value.lower()}",
                intent=intent,
                phase=phase,
                candidate_ids=tuple(item.candidate_id for item in ordered),
                capabilities=tuple(sorted({item.capability for item in ordered})),
                gap_ids=tuple(sorted({gap_id for item in ordered for gap_id in item.gap_ids})),
                dimensions=tuple(
                    sorted(
                        {dimension for item in ordered for dimension in item.dimensions},
                        key=lambda item: item.value,
                    )
                ),
                hypothesis_ids=tuple(
                    sorted(
                        {hypothesis_id for item in ordered for hypothesis_id in item.hypothesis_ids}
                    )
                ),
                alternative_ids=tuple(
                    sorted(
                        {
                            alternative_id
                            for item in ordered
                            for alternative_id in item.alternative_ids
                        }
                    )
                ),
            )
        )
    candidate_ids = {
        candidate.candidate_id
        for bundle in bundles
        for candidate in candidates
        if candidate.candidate_id in bundle.candidate_ids
    }
    admissible_ids = {
        candidate.candidate_id
        for candidate in candidates
        if is_observation_candidate_admissible(
            candidate,
            attempted_observations=attempted_observations,
            previous_investigations=previous_investigations,
        )
        and (
            classify_observation_candidate(candidate)
            is not InvestigationIntentKind.RUNTIME_DISCRIMINATION
            or _intent_allowed(
                InvestigationIntentKind.RUNTIME_DISCRIMINATION, phase, (candidate,), diagnosis
            )
        )
    }
    if candidate_ids != admissible_ids:
        raise AssertionError("semantic intent grouping lost or duplicated a physical candidate")
    return tuple(sorted(bundles, key=lambda item: item.intent.value))


def intent_utility_sort_key(scored: ScoredObservationBundle) -> tuple[object, ...]:
    utility = scored.utility
    return (
        not utility.admissible,
        -utility.phase_match,
        -utility.decision_blocker_match,
        -utility.leading_hypothesis_relevance,
        -utility.unresolved_hypothesis_relevance,
        -utility.eligibility_relevance,
        -utility.semantic_priority,
        utility.stable_tiebreak,
    )


def score_observation_bundle(
    *,
    bundle: ObservationBundle,
    phase: InvestigationPhase,
    diagnosis: Diagnosis,
    case: Case | None = None,
    source_actor_bundle_available: bool = True,
) -> ScoredObservationBundle:
    leading, unresolved = _hypothesis_relevance(bundle, diagnosis)
    eligibility = 0
    if case is not None:
        for hypothesis_id in bundle.hypothesis_ids:
            item = case.root_cause_eligibilities.for_hypothesis(hypothesis_id)
            if item is not None and item.state is RootCauseEligibilityState.UNDETERMINED:
                eligibility = max(eligibility, 1)
    utility = IntentUtility(
        admissible=True,
        phase_match=int(bundle.phase is phase),
        decision_blocker_match=_blocker_match(
            bundle, diagnosis, phase, source_actor_bundle_available
        ),
        leading_hypothesis_relevance=leading,
        unresolved_hypothesis_relevance=unresolved,
        eligibility_relevance=eligibility,
        semantic_priority=(
            _SOURCE_ORDER[bundle.intent]
            if phase is InvestigationPhase.SOURCE_DISCOVERY
            else _DISCRIMINATION_ORDER[bundle.intent]
        ),
        stable_tiebreak=bundle.bundle_id,
    )
    return ScoredObservationBundle(bundle=bundle, utility=utility)


def rank_observation_bundles(
    *,
    bundles: Sequence[ObservationBundle],
    phase: InvestigationPhase,
    diagnosis: Diagnosis,
    case: Case | None = None,
    source_actor_bundle_available: bool = True,
) -> tuple[ScoredObservationBundle, ...]:
    return tuple(
        sorted(
            (
                score_observation_bundle(
                    bundle=bundle,
                    phase=phase,
                    diagnosis=diagnosis,
                    case=case,
                    source_actor_bundle_available=source_actor_bundle_available,
                )
                for bundle in bundles
            ),
            key=intent_utility_sort_key,
        )
    )


def select_observation_bundle(
    *,
    case: Case,
    diagnosis: Diagnosis,
    candidates: Sequence[ObservationCandidate],
    attempted_observations: Sequence[str] = (),
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
) -> ScoredObservationBundle | None:
    phase = derive_investigation_phase(case, diagnosis)
    bundles = build_observation_bundles(
        case=case,
        diagnosis=diagnosis,
        candidates=candidates,
        attempted_observations=attempted_observations,
        previous_investigations=previous_investigations,
    )
    ranked = rank_observation_bundles(
        bundles=bundles,
        phase=phase,
        diagnosis=diagnosis,
        case=case,
        source_actor_bundle_available=any(
            bundle.intent is InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY for bundle in bundles
        ),
    )
    return ranked[0] if ranked else None


def select_observation_intent_candidate(
    *,
    case: Case,
    diagnosis: Diagnosis,
    engine_config: EngineConfig,
    attempted_observations: Sequence[str] = (),
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
    max_tool_calls_per_gap: int | None = None,
) -> SelectedObservationIntent | None:
    candidates = build_observation_candidates(
        case=case, diagnosis=diagnosis, engine_config=engine_config
    )
    phase = derive_investigation_phase(case, diagnosis)
    bundles = build_observation_bundles(
        case=case,
        diagnosis=diagnosis,
        candidates=candidates,
        attempted_observations=attempted_observations,
        previous_investigations=previous_investigations,
    )
    ranked_bundles = rank_observation_bundles(
        bundles=bundles,
        phase=phase,
        diagnosis=diagnosis,
        case=case,
        source_actor_bundle_available=any(
            bundle.intent is InvestigationIntentKind.INCIDENT_ACTOR_DISCOVERY for bundle in bundles
        ),
    )
    for scored_bundle in ranked_bundles:
        allowed = tuple(
            candidate
            for candidate in candidates
            if candidate.candidate_id in scored_bundle.bundle.candidate_ids
        )
        for scored in rank_observation_candidates(
            candidates=allowed,
            diagnosis=diagnosis,
            attempted_observations=attempted_observations,
            previous_investigations=previous_investigations,
        ):
            if (
                candidate_to_action(
                    scored,
                    diagnosis,
                    previous_investigations=previous_investigations,
                    max_tool_calls_per_gap=max_tool_calls_per_gap,
                )
                is not None
            ):
                return SelectedObservationIntent(
                    phase=phase, scored_bundle=scored_bundle, physical=scored
                )
    return None


@dataclass
class DeterministicIntentPolicy:
    """Marker policy whose action is supplied by the graph-bound intent selector."""

    counts_as_model: bool = False
    uses_intent_selector: bool = True

    def choose_action(self, _context: object) -> InvestigationAction:
        return InvestigationAction(action="stop", rationale="intent selector path required")


__all__ = [
    "DeterministicIntentPolicy",
    "IntentUtility",
    "InvestigationIntentKind",
    "InvestigationPhase",
    "ObservationBundle",
    "ScoredObservationBundle",
    "SelectedObservationIntent",
    "build_observation_bundles",
    "classify_observation_candidate",
    "derive_investigation_phase",
    "intent_utility_sort_key",
    "rank_observation_bundles",
    "score_observation_bundle",
    "select_observation_bundle",
    "select_observation_intent_candidate",
]
