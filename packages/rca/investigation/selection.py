"""Deterministic physical-observation utility and selection."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from packages.rca.engine import Case, EngineConfig
from packages.rca.investigation.actions import observation_identity
from packages.rca.investigation.candidates import (
    ObservationCandidate,
    build_observation_candidates,
)
from packages.rca.model import (
    Diagnosis,
    GapResolvability,
    InvestigationAction,
    InvestigationActionAudit,
    InvestigationExecutionStatus,
    InvestigationLedgerEntry,
    InvestigationQuery,
)

_OVERLAP_PREFERENCE = {
    "low": 3,
    "medium": 2,
    "high": 1,
}
_COST_TIER = {
    "history": 0,
    "events": 0,
    "logs": 1,
    "resource_pressure": 1,
    "traffic": 1,
    "runtime_traces": 2,
}


@dataclass(frozen=True)
class ObservationUtility:
    """Inspectable ordinal utility; fields are not probabilities or scores."""

    admissible: bool
    hypothesis_relevance: int
    structural_relevance: int
    discriminating_gap_coverage: int
    positive_discriminator_states: int
    competing_state_coverage: int
    discrimination_value: int
    expected_elimination_value: int
    frontier_coverage: int
    dimension_coverage: int
    overlap_preference: int
    shared_gap_coverage: int
    shared_alternative_coverage: int
    known_evidence_penalty: int
    semantic_duplicate_penalty: int
    no_data_repeat_penalty: int
    cost_tier: int
    stable_tiebreak: str


@dataclass(frozen=True)
class ScoredObservationCandidate:
    candidate: ObservationCandidate
    utility: ObservationUtility
    overlap_class: str


def observation_relevance_key(scored: ScoredObservationCandidate) -> tuple[int, ...]:
    """Return A6.2's relevance fields without its semantic-free tie-break."""
    utility = scored.utility
    return (
        utility.discrimination_value,
        utility.expected_elimination_value,
        -utility.semantic_duplicate_penalty,
        -utility.no_data_repeat_penalty,
        -utility.known_evidence_penalty,
        utility.frontier_coverage,
        -utility.cost_tier,
    )


def exploration_coverage_atoms(
    candidate: ObservationCandidate,
    diagnosis: Diagnosis,
) -> frozenset[tuple[str, str]]:
    """Return visible structural atoms covered by one exact physical read."""
    gaps = {
        gap.gap_id: gap
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE
    }
    return frozenset(
        (alternative_id, gaps[gap_id].dimension.value)
        for gap_id in candidate.gap_ids
        if gap_id in gaps
        for alternative_id in gaps[gap_id].alternative_ids
    )


def _unresolved_hypothesis_ids(diagnosis: Diagnosis) -> tuple[set[str], set[str]]:
    trace = diagnosis.resolution_trace
    if trace is None:
        return set(), set()
    leading = set(trace.leading_hypothesis_ids)
    unresolved = set(trace.unresolved_hypotheses)
    if not unresolved:
        unresolved.update(trace.plausible_hypotheses)
    return leading, unresolved


def _hypothesis_relevance(candidate: ObservationCandidate, diagnosis: Diagnosis) -> int:
    leading, unresolved = _unresolved_hypothesis_ids(diagnosis)
    candidate_ids = set(candidate.hypothesis_ids)
    if candidate_ids & leading:
        return 3
    if candidate_ids & unresolved:
        return 2
    return 1 if candidate_ids else 0


def _structural_relevance(candidate: ObservationCandidate, diagnosis: Diagnosis) -> int:
    alternatives = {item.alternative_id: item for item in diagnosis.structural_alternatives}
    referenced = {
        alternative_id
        for alternative_id in candidate.alternative_ids
        if alternative_id in alternatives
    }
    if any(alternatives[item].status.value == "UNEXPLORED" for item in referenced):
        return 2
    return 1 if referenced else 0


def _overlap_class(capability: str) -> str:
    if capability == "history":
        return "medium"
    return "low"


def _attempted_identities(
    *,
    attempted_observations: Sequence[str],
    previous_investigations: Sequence[InvestigationLedgerEntry],
) -> set[str]:
    identities = set(attempted_observations)
    for entry in previous_investigations:
        if entry.query is not None:
            identities.add(observation_identity(entry.capability, entry.target, entry.query))
    return identities


def _query_windows_overlap(
    candidate: ObservationCandidate, entry: InvestigationLedgerEntry
) -> bool:
    previous = entry.query
    if previous is None:
        return True
    left_start, right_start = candidate.query.start, previous.start
    left_end, right_end = candidate.query.end, previous.end
    if left_start is not None and right_end is not None and left_start > right_end:
        return False
    if right_start is not None and left_end is not None and right_start > left_end:
        return False
    return True


def _semantic_duplicate_penalty(
    candidate: ObservationCandidate,
    previous_investigations: Sequence[InvestigationLedgerEntry],
    previous_action_audits: Sequence[InvestigationActionAudit] = (),
) -> tuple[int, int]:
    """Count physical and semantic repeats of one bounded epistemic question.

    Exact physical repeats are excluded earlier by admissibility. This penalty
    also matches a prior successful action by capability, canonical target,
    gap dimension, supported/comparison states and bounded time-scope class.
    A prior NO_DATA response adds an explicit retry-risk count without changing
    any causal state.
    """
    current_identity = observation_identity(candidate.capability, candidate.target, candidate.query)
    physical_repeats = sum(
        entry.capability == candidate.capability
        and entry.target == candidate.target
        and entry.query is not None
        and observation_identity(entry.capability, entry.target, entry.query) != current_identity
        and _query_windows_overlap(candidate, entry)
        for entry in previous_investigations
    )
    current_fingerprints = _semantic_question_fingerprints(candidate)
    repeated = 0
    repeated_no_data = 0
    for audit in previous_action_audits:
        if (
            audit.authorization_result != "AUTHORIZED"
            or audit.backend_execution_status is not InvestigationExecutionStatus.SUCCEEDED
            or audit.action.capability != candidate.capability
            or audit.action.target != candidate.target
            or audit.gap_dimension is None
            or audit.discriminator is None
        ):
            continue
        previous_fingerprint = _audit_semantic_question_fingerprint(audit)
        if previous_fingerprint in current_fingerprints:
            repeated += 1
            repeated_no_data += int(
                audit.observation_outcome is not None
                and audit.observation_outcome.value == "NO_DATA"
            )
    return physical_repeats + repeated, repeated_no_data


def _query_scope_class(query: InvestigationQuery) -> str:
    if query.start is None or query.end is None:
        return "unbounded"
    seconds = max(0.0, (query.end - query.start).total_seconds())
    if seconds <= 15 * 60:
        return "short"
    if seconds <= 60 * 60:
        return "bounded"
    return "wide"


def _semantic_question_fingerprints(candidate: ObservationCandidate) -> set[tuple[object, ...]]:
    fingerprints: set[tuple[object, ...]] = set()
    for discriminator in candidate.discriminators:
        supported = tuple(
            sorted(
                (tuple(sorted(outcome.hypothesis_ids)), tuple(sorted(outcome.alternative_ids)))
                for outcome in discriminator.support_outcomes
            )
        )
        fingerprints.add(
            (
                candidate.capability,
                candidate.target.canonical,
                discriminator.dimension.value,
                supported,
                tuple(sorted(discriminator.comparison_hypothesis_ids)),
                tuple(sorted(discriminator.comparison_alternative_ids)),
                _query_scope_class(candidate.query),
            )
        )
    return fingerprints


def _audit_semantic_question_fingerprint(audit: InvestigationActionAudit) -> tuple[object, ...]:
    assert audit.action.capability is not None
    assert audit.action.target is not None
    discriminator = audit.discriminator
    assert discriminator is not None and audit.gap_dimension is not None
    supported = tuple(
        sorted(
            (tuple(sorted(outcome.hypothesis_ids)), tuple(sorted(outcome.alternative_ids)))
            for outcome in discriminator.support_outcomes
        )
    )
    return (
        audit.action.capability,
        audit.action.target.canonical,
        audit.gap_dimension.value,
        supported,
        tuple(sorted(discriminator.comparison_hypothesis_ids)),
        tuple(sorted(discriminator.comparison_alternative_ids)),
        _query_scope_class(audit.action.query or InvestigationQuery()),
    )


def _frontier_coverage(
    candidate: ObservationCandidate,
    previous_action_audits: Sequence[InvestigationActionAudit],
) -> int:
    queried = {
        (audit.action.capability, audit.action.target.canonical, audit.gap_dimension.value)
        for audit in previous_action_audits
        if audit.authorization_result == "AUTHORIZED"
        and audit.backend_execution_status is InvestigationExecutionStatus.SUCCEEDED
        and audit.action.capability is not None
        and audit.action.target is not None
        and audit.gap_dimension is not None
    }
    return sum(
        (candidate.capability, candidate.target.canonical, discriminator.dimension.value)
        not in queried
        for discriminator in candidate.discriminators
    )


def _discrimination_value(candidate: ObservationCandidate) -> int:
    pairs: set[tuple[str, str]] = set()
    for discriminator in candidate.discriminators:
        supports = {
            f"hypothesis:{identifier}"
            for outcome in discriminator.support_outcomes
            for identifier in outcome.hypothesis_ids
        } | {
            f"alternative:{identifier}"
            for outcome in discriminator.support_outcomes
            for identifier in outcome.alternative_ids
        }
        comparisons = {
            *(f"hypothesis:{identifier}" for identifier in discriminator.comparison_hypothesis_ids),
            *(
                f"alternative:{identifier}"
                for identifier in discriminator.comparison_alternative_ids
            ),
        }
        pairs.update((left, right) for left in supports for right in comparisons if left != right)
    return len(pairs)


def _expected_elimination_value(candidate: ObservationCandidate, diagnosis: Diagnosis) -> int:
    leading = (
        set(diagnosis.resolution_trace.leading_hypothesis_ids)
        if diagnosis.resolution_trace
        else set()
    )
    supports_hypothesis = {
        identifier
        for discriminator in candidate.discriminators
        for outcome in discriminator.support_outcomes
        for identifier in outcome.hypothesis_ids
    }
    supports_alternative = {
        identifier
        for discriminator in candidate.discriminators
        for outcome in discriminator.support_outcomes
        for identifier in outcome.alternative_ids
    }
    compares_hypothesis = {
        identifier
        for discriminator in candidate.discriminators
        for identifier in discriminator.comparison_hypothesis_ids
    }
    compares_alternative = {
        identifier
        for discriminator in candidate.discriminators
        for identifier in discriminator.comparison_alternative_ids
    }
    if supports_hypothesis & leading and (compares_hypothesis or compares_alternative):
        return 4
    if supports_alternative and compares_alternative:
        return 3
    if supports_hypothesis and compares_hypothesis:
        return 2
    if supports_hypothesis or supports_alternative:
        return 1
    return 0


def is_observation_candidate_admissible(
    candidate: ObservationCandidate,
    *,
    attempted_observations: Sequence[str] = (),
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
) -> bool:
    """Return whether one exact physical read has not already been attempted."""
    attempted = _attempted_identities(
        attempted_observations=attempted_observations,
        previous_investigations=previous_investigations,
    )
    return (
        observation_identity(candidate.capability, candidate.target, candidate.query)
        not in attempted
    )


def score_observation_candidate(
    *,
    candidate: ObservationCandidate,
    diagnosis: Diagnosis,
    attempted_observations: Sequence[str] = (),
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
    previous_action_audits: Sequence[InvestigationActionAudit] = (),
) -> ScoredObservationCandidate:
    """Assign a deterministic utility vector using only visible state."""
    attempted = _attempted_identities(
        attempted_observations=attempted_observations,
        previous_investigations=previous_investigations,
    )
    physical_identity = observation_identity(
        candidate.capability, candidate.target, candidate.query
    )
    admissible = physical_identity not in attempted
    current_gaps = {
        gap.gap_id: gap
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE
    }
    covered_gap_ids = set(candidate.gap_ids) & set(current_gaps)
    covered_dimensions = {current_gaps[gap_id].dimension for gap_id in covered_gap_ids}
    covered_alternatives = {
        alternative_id
        for gap_id in covered_gap_ids
        for alternative_id in current_gaps[gap_id].alternative_ids
    }
    overlap_class = _overlap_class(candidate.capability)
    discriminators = tuple(
        item for item in candidate.discriminators if item.gap_id in covered_gap_ids
    )
    positive_states = {
        state_id
        for discriminator in discriminators
        for outcome in discriminator.support_outcomes
        for state_id in (*outcome.hypothesis_ids, *outcome.alternative_ids)
    }
    competing_states = {
        state_id
        for discriminator in discriminators
        for state_id in (
            *discriminator.comparison_hypothesis_ids,
            *discriminator.comparison_alternative_ids,
        )
    }
    previous_known_refs = {
        ref
        for entry in previous_investigations
        if entry.capability == candidate.capability and entry.target == candidate.target
        for ref in entry.already_known_refs
    }
    known_evidence_penalty = len(candidate.known_fact_keys) + len(
        set(candidate.known_evidence_refs) | previous_known_refs
    )
    semantic_duplicate_penalty, no_data_repeat_penalty = _semantic_duplicate_penalty(
        candidate, previous_investigations, previous_action_audits
    )
    utility = ObservationUtility(
        admissible=admissible,
        hypothesis_relevance=_hypothesis_relevance(candidate, diagnosis),
        structural_relevance=_structural_relevance(candidate, diagnosis),
        discriminating_gap_coverage=len({item.gap_id for item in discriminators}),
        positive_discriminator_states=len(positive_states),
        competing_state_coverage=len(competing_states),
        discrimination_value=_discrimination_value(candidate),
        expected_elimination_value=_expected_elimination_value(candidate, diagnosis),
        frontier_coverage=_frontier_coverage(candidate, previous_action_audits),
        dimension_coverage=len(covered_dimensions),
        overlap_preference=_OVERLAP_PREFERENCE[overlap_class],
        shared_gap_coverage=max(0, len(covered_gap_ids) - 1),
        shared_alternative_coverage=max(0, len(covered_alternatives) - 1),
        known_evidence_penalty=known_evidence_penalty,
        semantic_duplicate_penalty=semantic_duplicate_penalty,
        no_data_repeat_penalty=no_data_repeat_penalty,
        cost_tier=_COST_TIER.get(candidate.capability, 2),
        stable_tiebreak=candidate.candidate_id,
    )
    return ScoredObservationCandidate(
        candidate=candidate,
        utility=utility,
        overlap_class=overlap_class,
    )


def utility_sort_key(scored: ScoredObservationCandidate) -> tuple[object, ...]:
    """Order by active-diagnosis value before redundancy, novelty and cost."""
    utility = scored.utility
    return (
        not utility.admissible,
        -utility.discrimination_value,
        -utility.expected_elimination_value,
        utility.semantic_duplicate_penalty,
        utility.no_data_repeat_penalty,
        utility.known_evidence_penalty,
        -utility.frontier_coverage,
        utility.cost_tier,
        utility.stable_tiebreak,
    )


def rank_observation_candidates(
    *,
    candidates: Sequence[ObservationCandidate],
    diagnosis: Diagnosis,
    attempted_observations: Sequence[str] = (),
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
    previous_action_audits: Sequence[InvestigationActionAudit] = (),
    require_discriminator: bool = False,
) -> tuple[ScoredObservationCandidate, ...]:
    """Score and order physical candidates without performing a read."""
    scored = tuple(
        score_observation_candidate(
            candidate=candidate,
            diagnosis=diagnosis,
            attempted_observations=attempted_observations,
            previous_investigations=previous_investigations,
            previous_action_audits=previous_action_audits,
        )
        for candidate in candidates
    )
    return tuple(
        sorted(
            (
                item
                for item in scored
                if item.utility.admissible
                and (not require_discriminator or item.utility.discriminating_gap_coverage > 0)
            ),
            key=utility_sort_key,
        )
    )


def _representative_gap(
    candidate: ObservationCandidate,
    diagnosis: Diagnosis,
) -> str | None:
    leading, unresolved = _unresolved_hypothesis_ids(diagnosis)
    alternatives = {item.alternative_id: item for item in diagnosis.structural_alternatives}
    gaps = {
        gap.gap_id: gap
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE and gap.gap_id in candidate.gap_ids
    }
    if not gaps:
        return None

    def key(gap_id: str) -> tuple[int, int, int, str]:
        gap = gaps[gap_id]
        hypothesis_score = (
            2
            if set(gap.hypothesis_ids) & leading
            else 1
            if set(gap.hypothesis_ids) & unresolved
            else 0
        )
        structural_score = int(
            any(
                alternative_id in alternatives
                and alternatives[alternative_id].status.value == "UNEXPLORED"
                for alternative_id in gap.alternative_ids
            )
        )
        discriminator_score = int(any(item.gap_id == gap_id for item in candidate.discriminators))
        return (-discriminator_score, -hypothesis_score, -structural_score, gap_id)

    return min(gaps, key=key)


def candidate_to_action(
    scored_candidate: ScoredObservationCandidate,
    diagnosis: Diagnosis,
    *,
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
    max_tool_calls_per_gap: int | None = None,
) -> InvestigationAction | None:
    """Convert one selected physical read to the existing action contract."""
    candidate = scored_candidate.candidate
    gap_ids = [
        gap_id
        for gap_id in candidate.gap_ids
        if max_tool_calls_per_gap is None
        or sum(entry.gap_id == gap_id for entry in previous_investigations) < max_tool_calls_per_gap
    ]
    if not gap_ids:
        return None
    executable_candidate = (
        candidate
        if tuple(gap_ids) == candidate.gap_ids
        else ObservationCandidate(
            candidate_id=candidate.candidate_id,
            capability=candidate.capability,
            target=candidate.target,
            query=candidate.query,
            gap_ids=tuple(gap_ids),
            dimensions=candidate.dimensions,
            hypothesis_ids=candidate.hypothesis_ids,
            alternative_ids=candidate.alternative_ids,
            discriminators=tuple(
                item for item in candidate.discriminators if item.gap_id in gap_ids
            ),
        )
    )
    gap_id = _representative_gap(executable_candidate, diagnosis)
    if gap_id is None:
        return None
    utility = scored_candidate.utility
    discriminator = next((item for item in candidate.discriminators if item.gap_id == gap_id), None)
    rationale = (
        f"deterministic candidate {candidate.candidate_id}; "
        f"utility={utility.hypothesis_relevance}/"
        f"{utility.structural_relevance}/"
        f"{utility.discriminating_gap_coverage}/"
        f"{utility.dimension_coverage}"
    )
    if discriminator is not None:
        supported_count = len(
            {
                state_id
                for outcome in discriminator.support_outcomes
                for state_id in (*outcome.hypothesis_ids, *outcome.alternative_ids)
            }
        )
        competing_count = len(
            {
                *discriminator.comparison_hypothesis_ids,
                *discriminator.comparison_alternative_ids,
            }
        )
        rationale += (
            f"; discriminator gap={gap_id}; positive-states={supported_count}; "
            f"competing-states={competing_count}; NO_DATA/UNKNOWN are non-discriminating"
        )
    return InvestigationAction(
        action="inspect",
        gap_id=gap_id,
        capability=candidate.capability,
        target=candidate.target,
        query=candidate.query,
        rationale=rationale,
    )


def select_observation_candidate(
    *,
    case: Case,
    diagnosis: Diagnosis,
    engine_config: EngineConfig,
    attempted_observations: Sequence[str] = (),
    previous_investigations: Sequence[InvestigationLedgerEntry] = (),
    previous_action_audits: Sequence[InvestigationActionAudit] = (),
    max_tool_calls_per_gap: int | None = None,
    require_discriminator: bool = False,
) -> ScoredObservationCandidate | None:
    """Build, rank, and select one currently admissible physical read."""
    candidates = build_observation_candidates(
        case=case,
        diagnosis=diagnosis,
        engine_config=engine_config,
    )
    ranked = rank_observation_candidates(
        candidates=candidates,
        diagnosis=diagnosis,
        attempted_observations=attempted_observations,
        previous_investigations=previous_investigations,
        previous_action_audits=previous_action_audits,
        require_discriminator=require_discriminator,
    )
    for item in ranked:
        if (
            candidate_to_action(
                item,
                diagnosis,
                previous_investigations=previous_investigations,
                max_tool_calls_per_gap=max_tool_calls_per_gap,
            )
            is not None
        ):
            return item
    return None


@dataclass
class DeterministicObservationPolicy:
    """Marker policy whose action is supplied by the graph-bound selector."""

    counts_as_model: bool = False
    uses_candidate_selector: bool = True

    def choose_action(self, _context: object) -> InvestigationAction:
        return InvestigationAction(action="stop", rationale="selector path required")


__all__ = [
    "DeterministicObservationPolicy",
    "ObservationUtility",
    "ScoredObservationCandidate",
    "candidate_to_action",
    "exploration_coverage_atoms",
    "is_observation_candidate_admissible",
    "observation_relevance_key",
    "rank_observation_candidates",
    "score_observation_candidate",
    "select_observation_candidate",
    "utility_sort_key",
]
