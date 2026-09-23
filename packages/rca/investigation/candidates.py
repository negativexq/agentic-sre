"""Deterministic physical observation candidates for bounded investigation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256

from packages.rca.engine import Case, EngineConfig
from packages.rca.investigation.actions import observation_identity
from packages.rca.model import (
    Diagnosis,
    EntityRef,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    GapResolvability,
    InformationGap,
    InvestigationQuery,
)

_BOUNDED_WINDOW = timedelta(minutes=30)
_MAX_PROVIDER_WINDOW = timedelta(hours=1)
_CANDIDATE_LIMIT = 32


@dataclass(frozen=True)
class CandidateDiscriminator:
    """One positive observation that can distinguish a candidate state.

    ``comparison_*`` identifies other currently plausible states that remain
    viable if this candidate-specific positive observation is absent. A
    NO_DATA outcome is deliberately retained as non-discriminating context.
    """

    gap_id: str
    dimension: GapDimension
    missing_fact: str
    support_outcomes: tuple[GapOutcome, ...]
    comparison_hypothesis_ids: tuple[str, ...]
    comparison_alternative_ids: tuple[str, ...]
    no_data_outcomes: tuple[GapOutcome, ...]


@dataclass(frozen=True)
class ObservationCandidate:
    """One exact physical read and the logical needs it can answer."""

    candidate_id: str
    capability: str
    target: EntityRef
    query: InvestigationQuery
    gap_ids: tuple[str, ...]
    dimensions: tuple[GapDimension, ...]
    hypothesis_ids: tuple[str, ...]
    alternative_ids: tuple[str, ...]
    discriminators: tuple[CandidateDiscriminator, ...] = ()


@dataclass
class _CandidateAccumulator:
    capability: str
    target: EntityRef
    query: InvestigationQuery
    gap_ids: set[str] = field(default_factory=set)
    dimensions: set[GapDimension] = field(default_factory=set)
    hypothesis_ids: set[str] = field(default_factory=set)
    alternative_ids: set[str] = field(default_factory=set)
    discriminators: dict[str, CandidateDiscriminator] = field(default_factory=dict)


def _is_usable_anchor(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() is not None


def _time_anchor(case: Case) -> datetime | None:
    cutoff = case.source.observation_cutoff()
    for value in (case.symptoms.onset, case.context.window_end, cutoff):
        if _is_usable_anchor(value):
            return value
    return None


def _observation_cutoff(case: Case) -> datetime | None:
    cutoff = case.source.observation_cutoff()
    return cutoff if _is_usable_anchor(cutoff) else None


def _bounded_query(
    *,
    start: datetime,
    end: datetime,
    cutoff: datetime | None,
    limit: int = _CANDIDATE_LIMIT,
    provider_bounded: bool = False,
) -> InvestigationQuery | None:
    if cutoff is not None:
        end = min(end, cutoff)
    if end < start:
        return None
    if provider_bounded and end - start > _MAX_PROVIDER_WINDOW:
        return None
    return InvestigationQuery(start=start, end=end, limit=limit)


def resolve_effective_query(
    *,
    capability: str,
    case: Case,
    engine_config: EngineConfig,
) -> InvestigationQuery | None:
    """Resolve one explicit bounded query without reading incident telemetry."""
    raw_cutoff = case.source.observation_cutoff()
    if raw_cutoff is not None and not _is_usable_anchor(raw_cutoff):
        return None
    anchor = _time_anchor(case)
    if anchor is None:
        return None
    cutoff = _observation_cutoff(case)
    onset = case.symptoms.onset if _is_usable_anchor(case.symptoms.onset) else None
    if onset is not None:
        causal_anchor = onset
    else:
        causal_anchor = anchor

    if capability in {"history", "events"}:
        return _bounded_query(
            start=causal_anchor - engine_config.ranking.lookback,
            end=causal_anchor + engine_config.ranking.grace,
            cutoff=cutoff,
        )
    if capability in {"incident_events", "incident_changes"}:
        return _bounded_query(
            start=causal_anchor - engine_config.ranking.lookback,
            end=causal_anchor + engine_config.ranking.grace,
            cutoff=cutoff,
            limit=64,
        )
    if capability in {"logs", "runtime_traces", "resource_pressure", "traffic"}:
        return _bounded_query(
            start=anchor - _BOUNDED_WINDOW,
            end=anchor + _BOUNDED_WINDOW,
            cutoff=cutoff,
            provider_bounded=capability != "logs",
        )
    return None


def _candidate_id(identity: str) -> str:
    return f"candidate:{sha256(identity.encode()).hexdigest()[:20]}"


def _query_sort_key(query: InvestigationQuery) -> tuple[str, str, int]:
    return (
        query.start.isoformat() if query.start is not None else "",
        query.end.isoformat() if query.end is not None else "",
        query.limit,
    )


def _plausible_state_ids(diagnosis: Diagnosis) -> tuple[set[str], set[str]]:
    trace = diagnosis.resolution_trace
    hypothesis_ids: set[str] = set()
    if trace is not None:
        hypothesis_ids.update(trace.leading_hypothesis_ids)
        hypothesis_ids.update(trace.unresolved_hypotheses)
        hypothesis_ids.update(trace.plausible_hypotheses)
    if not hypothesis_ids:
        hypothesis_ids.update(
            hypothesis.hypothesis_id
            for hypothesis in (
                *((diagnosis.hypothesis,) if diagnosis.hypothesis is not None else ()),
                *diagnosis.ambiguous_hypotheses,
                *diagnosis.alternative_hypotheses,
            )
        )
    alternative_ids = {
        alternative.alternative_id
        for alternative in diagnosis.structural_alternatives
        if alternative.status.value == "UNEXPLORED"
    }
    return hypothesis_ids, alternative_ids


def _candidate_discriminator(
    gap: InformationGap,
    *,
    plausible_hypothesis_ids: set[str],
    plausible_alternative_ids: set[str],
    target_hypothesis_ids: set[str] | None = None,
    target_alternative_ids: set[str] | None = None,
) -> CandidateDiscriminator | None:
    supports = tuple(
        outcome
        for outcome in gap.discriminating_outcomes
        if outcome.kind is GapOutcomeKind.SUPPORTS
        and (
            set(outcome.hypothesis_ids)
            & (
                target_hypothesis_ids
                if target_hypothesis_ids is not None
                else plausible_hypothesis_ids
            )
            or set(outcome.alternative_ids)
            & (
                target_alternative_ids
                if target_alternative_ids is not None
                else plausible_alternative_ids
            )
        )
    )
    supported_hypothesis_ids = {
        hypothesis_id
        for outcome in supports
        for hypothesis_id in outcome.hypothesis_ids
        if hypothesis_id in plausible_hypothesis_ids
    }
    supported_alternative_ids = {
        alternative_id
        for outcome in supports
        for alternative_id in outcome.alternative_ids
        if alternative_id in plausible_alternative_ids
    }
    comparison_hypothesis_ids = plausible_hypothesis_ids - supported_hypothesis_ids
    comparison_alternative_ids = plausible_alternative_ids - supported_alternative_ids
    if not (supported_hypothesis_ids or supported_alternative_ids):
        return None
    if not (comparison_hypothesis_ids or comparison_alternative_ids):
        return None
    return CandidateDiscriminator(
        gap_id=gap.gap_id,
        dimension=gap.dimension,
        missing_fact=gap.missing_fact,
        support_outcomes=supports,
        comparison_hypothesis_ids=tuple(sorted(comparison_hypothesis_ids)),
        comparison_alternative_ids=tuple(sorted(comparison_alternative_ids)),
        no_data_outcomes=tuple(
            outcome
            for outcome in gap.discriminating_outcomes
            if outcome.kind is GapOutcomeKind.NO_DATA
        ),
    )


def build_observation_candidates(
    *,
    case: Case,
    diagnosis: Diagnosis,
    engine_config: EngineConfig,
) -> tuple[ObservationCandidate, ...]:
    """Coalesce resolvable logical needs into explicit physical reads."""
    accumulators: dict[str, _CandidateAccumulator] = {}
    plausible_hypothesis_ids, plausible_alternative_ids = _plausible_state_ids(diagnosis)
    hypotheses = (
        *((diagnosis.hypothesis,) if diagnosis.hypothesis is not None else ()),
        *diagnosis.ambiguous_hypotheses,
        *diagnosis.alternative_hypotheses,
    )
    for gap in diagnosis.information_gaps:
        if gap.resolvability is not GapResolvability.RESOLVABLE:
            continue
        targets_by_capability: dict[str, set[str]] = {}
        for authorized_query in gap.authorized_queries:
            targets_by_capability.setdefault(authorized_query.capability, set()).add(
                authorized_query.target.canonical
            )
        for authorized in gap.authorized_queries:
            query = resolve_effective_query(
                capability=authorized.capability,
                case=case,
                engine_config=engine_config,
            )
            if query is None:
                continue
            identity = observation_identity(authorized.capability, authorized.target, query)
            accumulator = accumulators.get(identity)
            if accumulator is None:
                accumulator = _CandidateAccumulator(
                    capability=authorized.capability,
                    target=authorized.target,
                    query=query,
                )
                accumulators[identity] = accumulator
            accumulator.gap_ids.add(gap.gap_id)
            accumulator.dimensions.add(gap.dimension)
            accumulator.hypothesis_ids.update(gap.hypothesis_ids)
            accumulator.alternative_ids.update(gap.alternative_ids)
            target_specific = len(targets_by_capability[authorized.capability]) > 1
            if target_specific:
                target_hypothesis_ids = {
                    hypothesis.hypothesis_id
                    for hypothesis in hypotheses
                    if hypothesis.causal_actor == authorized.target
                }
                target_alternative_ids = set(authorized.alternative_ids) | {
                    alternative.alternative_id
                    for alternative in diagnosis.structural_alternatives
                    if alternative.actor == authorized.target
                    or authorized.target in alternative.observation_targets
                }
            else:
                target_hypothesis_ids = None
                target_alternative_ids = None
            discriminator = _candidate_discriminator(
                gap,
                plausible_hypothesis_ids=plausible_hypothesis_ids,
                plausible_alternative_ids=plausible_alternative_ids,
                target_hypothesis_ids=target_hypothesis_ids,
                target_alternative_ids=target_alternative_ids,
            )
            if discriminator is not None:
                accumulator.discriminators[gap.gap_id] = discriminator

    candidates = [
        ObservationCandidate(
            candidate_id=_candidate_id(identity),
            capability=accumulator.capability,
            target=accumulator.target,
            query=accumulator.query,
            gap_ids=tuple(sorted(accumulator.gap_ids)),
            dimensions=tuple(sorted(accumulator.dimensions, key=lambda item: item.value)),
            hypothesis_ids=tuple(sorted(accumulator.hypothesis_ids)),
            alternative_ids=tuple(sorted(accumulator.alternative_ids)),
            discriminators=tuple(
                accumulator.discriminators[gap_id] for gap_id in sorted(accumulator.discriminators)
            ),
        )
        for identity, accumulator in accumulators.items()
    ]
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.capability,
                candidate.target.canonical,
                *_query_sort_key(candidate.query),
                candidate.candidate_id,
            ),
        )
    )


__all__ = [
    "CandidateDiscriminator",
    "ObservationCandidate",
    "build_observation_candidates",
    "resolve_effective_query",
]
