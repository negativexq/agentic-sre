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
    GapResolvability,
    InvestigationQuery,
)

_BOUNDED_WINDOW = timedelta(minutes=30)
_MAX_PROVIDER_WINDOW = timedelta(hours=1)
_CANDIDATE_LIMIT = 32


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


@dataclass
class _CandidateAccumulator:
    capability: str
    target: EntityRef
    query: InvestigationQuery
    gap_ids: set[str] = field(default_factory=set)
    dimensions: set[GapDimension] = field(default_factory=set)
    hypothesis_ids: set[str] = field(default_factory=set)
    alternative_ids: set[str] = field(default_factory=set)


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


def build_observation_candidates(
    *,
    case: Case,
    diagnosis: Diagnosis,
    engine_config: EngineConfig,
) -> tuple[ObservationCandidate, ...]:
    """Coalesce resolvable logical needs into explicit physical reads."""
    accumulators: dict[str, _CandidateAccumulator] = {}
    for gap in diagnosis.information_gaps:
        if gap.resolvability is not GapResolvability.RESOLVABLE:
            continue
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
    "ObservationCandidate",
    "build_observation_candidates",
    "resolve_effective_query",
]
