"""Deterministic bounded search over legal investigation queries."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any

from packages.rca.engine import Case, build_case, diagnose_case
from packages.rca.frontier import apply_frontier_progress, covered_frontier_dimensions
from packages.rca.investigation.actions import observation_identity
from packages.rca.investigation.environment import (
    SeedPolicy,
    initial_view,
    investigation_backend,
)
from packages.rca.investigation.normalizers import (
    deduplicate_findings,
    new_investigation_findings,
    normalize_observation,
)
from packages.rca.investigation.state import InvestigationTool
from packages.rca.investigation.tools import default_tools
from packages.rca.model import (
    Diagnosis,
    EntityRef,
    Finding,
    GapDimension,
    GapResolvability,
    Hypothesis,
    InformationGap,
    InvestigationQuery,
    Resolution,
)
from packages.rca.ranking import RankingConfig
from packages.rca.signals import extract_symptoms
from packages.rca.source import ObservationSource


@dataclass(frozen=True)
class SearchStep:
    """One legal query and its deterministic before/after measurements."""

    depth: int
    gap_id: str
    dimension: str
    capability: str
    target: str
    raw_records: int
    payload_fields: tuple[str, ...]
    returned_refs: tuple[str, ...]
    new_refs: tuple[str, ...]
    normalized_findings: tuple[str, ...]
    new_findings: tuple[str, ...]
    hypotheses_before: int
    hypotheses_after: int
    hypotheses_changed: bool
    resolution_before: str
    resolution_after: str
    query_template: str = "full-history"
    observation_identity: str = ""
    attempt_outcome: str = ""
    frontier_changed: bool = False


@dataclass(frozen=True)
class LegalQueryChoice:
    """One exact authorized telemetry read and its deterministic template."""

    gap: InformationGap
    capability: str
    target: EntityRef
    alternative_ids: tuple[str, ...]
    query_template: str
    query: InvestigationQuery
    observation_identity: str


@dataclass(frozen=True)
class SearchPath:
    """A branch that reached a deterministic resolution."""

    steps: tuple[SearchStep, ...]
    diagnosis: Diagnosis


@dataclass(frozen=True)
class MultiStepSearchResult:
    """Bounded search measurements for one incident."""

    incident_id: str
    seed_policy: str
    initial_case: Case
    initial_diagnosis: Diagnosis
    attempts: tuple[SearchStep, ...]
    solutions: tuple[SearchPath, ...]
    max_depth: int
    explored_states: int
    truncated: bool

    @property
    def minimum_queries(self) -> int | None:
        return min((len(path.steps) for path in self.solutions), default=None)

    @property
    def minimum_solution(self) -> SearchPath | None:
        return min(self.solutions, key=lambda path: len(path.steps)) if self.solutions else None

    def resolvable_within(self, depth: int) -> bool:
        minimum = self.minimum_queries
        return minimum is not None and minimum <= depth


@dataclass(frozen=True)
class _State:
    case: Case
    diagnosis: Diagnosis
    findings: tuple[Finding, ...]
    steps: tuple[SearchStep, ...]
    query_keys: frozenset[str]
    queried_dimensions: tuple[tuple[str, tuple[str, ...]], ...]
    depth: int


_MAX_LIMIT = 64
_ONSET_WINDOWS: tuple[tuple[str, timedelta], ...] = (
    ("onset+-5m", timedelta(minutes=5)),
    ("onset+-15m", timedelta(minutes=15)),
)


def query_templates(source: ObservationSource) -> tuple[tuple[str, InvestigationQuery], ...]:
    """Return generic, incident-independent investigation query templates."""
    cutoff = source.observation_cutoff()
    templates: list[tuple[str, InvestigationQuery]] = [
        ("full-history", InvestigationQuery(end=cutoff, limit=_MAX_LIMIT))
    ]
    onset = extract_symptoms(source.alerts()).onset
    if onset is None:
        return tuple(templates)

    def bounded(end: datetime) -> datetime:
        return min(end, cutoff) if cutoff is not None else end

    for label, delta in _ONSET_WINDOWS:
        templates.append(
            (
                label,
                InvestigationQuery(
                    start=onset - delta, end=bounded(onset + delta), limit=_MAX_LIMIT
                ),
            )
        )
    templates.append(
        (
            "incident-window",
            InvestigationQuery(
                start=onset - RankingConfig().lookback, end=cutoff, limit=_MAX_LIMIT
            ),
        )
    )
    return tuple(templates)


def _raw_record_count(payload: Mapping[str, Any]) -> int:
    for key in ("versions", "events", "logs", "resource_pressure", "traffic"):
        records = payload.get(key)
        if isinstance(records, list):
            return len(records)
    return 0


def _finding_key(finding: Finding) -> str:
    return json.dumps(
        {
            "entity": finding.entity.canonical,
            "kind": finding.kind.value,
            "at": finding.at.isoformat() if finding.at else None,
            "summary": finding.summary,
            "details": finding.details,
            "evidence": sorted(finding.evidence_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _hypothesis_key(item: Hypothesis) -> str:
    return json.dumps(
        {
            "id": item.hypothesis_id,
            "actor": item.causal_actor.canonical,
            "members": sorted(entity.canonical for entity in item.members),
            "findings": sorted(_finding_key(finding) for finding in item.findings),
            "structural": sorted(item.structural_basis),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _frontier_key(case: Case) -> tuple[tuple[str, tuple[str, ...], str], ...]:
    return tuple(
        sorted(
            (
                item.alternative_id,
                tuple(dimension.value for dimension in item.queried_dimensions),
                item.status.value,
            )
            for item in case.structural_alternatives
        )
    )


def state_fingerprint(
    case: Case,
    diagnosis: Diagnosis,
    queried_dimensions: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> str:
    """Fingerprint deterministic evidence, resolution, and frontier state."""
    material = {
        "evidence_refs": sorted(
            evidence_id for finding in case.findings for evidence_id in finding.evidence_ids
        ),
        "findings": sorted(_finding_key(finding) for finding in case.findings),
        "hypotheses": sorted(_hypothesis_key(hypothesis) for hypothesis in case.hypotheses),
        "resolution": diagnosis.resolution.value,
        "leading": sorted(
            diagnosis.resolution_trace.leading_hypothesis_ids
            if diagnosis.resolution_trace is not None
            else ()
        ),
        "frontier": _frontier_key(case),
        "queried_dimensions": tuple(sorted(queried_dimensions)),
    }
    return sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def _resolvable_gaps(diagnosis: Diagnosis) -> tuple[InformationGap, ...]:
    return tuple(
        gap
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE and gap.authorized_queries
    )


def legal_query_choices(
    diagnosis: Diagnosis,
    tools: Mapping[str, InvestigationTool],
    templates: tuple[tuple[str, InvestigationQuery], ...],
    attempted_observations: Collection[str] = (),
) -> tuple[LegalQueryChoice, ...]:
    """Enumerate each exact, authorized, unseen telemetry read once."""
    attempted = set(attempted_observations)
    grouped: dict[str, list[LegalQueryChoice]] = {}
    for gap in sorted(
        _resolvable_gaps(diagnosis), key=lambda item: (item.dimension.value, item.gap_id)
    ):
        for authorized in sorted(
            gap.authorized_queries,
            key=lambda item: (item.capability, item.target.canonical),
        ):
            if authorized.capability not in tools:
                continue
            for label, query in templates:
                identity = observation_identity(authorized.capability, authorized.target, query)
                if identity in attempted:
                    continue
                grouped.setdefault(identity, []).append(
                    LegalQueryChoice(
                        gap=gap,
                        capability=authorized.capability,
                        target=authorized.target,
                        alternative_ids=authorized.alternative_ids,
                        query_template=label,
                        query=query,
                        observation_identity=identity,
                    )
                )
    choices: list[LegalQueryChoice] = []
    for identity in sorted(grouped):
        candidates = grouped[identity]
        representative = min(
            candidates,
            key=lambda item: (
                item.gap.dimension.value,
                item.gap.gap_id,
                item.capability,
                item.target.canonical,
                item.query_template,
            ),
        )
        alternatives = tuple(
            sorted(
                {alternative_id for item in candidates for alternative_id in item.alternative_ids}
            )
        )
        choices.append(replace(representative, alternative_ids=alternatives))
    return tuple(choices)


def _merge_queried_dimensions(
    current: tuple[tuple[str, tuple[str, ...]], ...],
    additions: Mapping[str, Collection[GapDimension]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    merged: dict[str, set[str]] = {key: set(values) for key, values in current}
    for alternative_id, dimensions in additions.items():
        merged.setdefault(alternative_id, set()).update(dimension.value for dimension in dimensions)
    return tuple(sorted((key, tuple(sorted(values))) for key, values in merged.items()))


def _rebuild_search_case(
    bounded: ObservationSource,
    findings: tuple[Finding, ...],
    queried_dimensions: tuple[tuple[str, tuple[str, ...]], ...],
    diagnose: Callable[[Case], Diagnosis],
) -> tuple[Case, Diagnosis]:
    case = build_case(bounded, extra_findings=findings)
    dimensions = {
        alternative_id: tuple(GapDimension(value) for value in values)
        for alternative_id, values in queried_dimensions
    }
    case.structural_alternatives = list(
        apply_frontier_progress(
            tuple(case.structural_alternatives),
            hypotheses=case.hypotheses,
            queried_dimensions_by_alternative=dimensions,
        )
    )
    return case, diagnose(case)


def _step(
    depth: int,
    case: Case,
    diagnosis: Diagnosis,
    choice: LegalQueryChoice,
    tool: InvestigationTool,
) -> tuple[SearchStep, tuple[Finding, ...], Any]:
    execute_query = getattr(tool, "execute_query", None)
    try:
        observation = (
            execute_query(case, choice.gap, choice.target, choice.query)
            if callable(execute_query)
            else tool.execute(case, choice.gap, choice.target)
        )
    except Exception as error:
        step = SearchStep(
            depth=depth,
            gap_id=choice.gap.gap_id,
            dimension=choice.gap.dimension.value,
            capability=choice.capability,
            target=choice.target.canonical,
            raw_records=0,
            payload_fields=(),
            returned_refs=(),
            new_refs=(),
            normalized_findings=(),
            new_findings=(),
            hypotheses_before=len(case.hypotheses),
            hypotheses_after=len(case.hypotheses),
            hypotheses_changed=False,
            resolution_before=diagnosis.resolution.value,
            resolution_after=diagnosis.resolution.value,
            query_template=choice.query_template,
            observation_identity=choice.observation_identity,
            attempt_outcome=f"TOOL_ERROR:{type(error).__name__}",
        )
        return step, (), None
    normalized = normalize_observation(observation, case=case, gap=choice.gap)
    fresh = new_investigation_findings(case.findings, normalized.findings)
    returned_refs = tuple(dict.fromkeys(observation.evidence_refs))
    known_refs = {evidence_id for finding in case.findings for evidence_id in finding.evidence_ids}
    new_refs = tuple(ref for ref in returned_refs if ref not in known_refs)
    if observation.error:
        outcome = "TOOL_ERROR"
    elif _raw_record_count(observation.payload) == 0:
        outcome = "NO_RAW_RECORDS"
    elif not new_refs:
        outcome = "ALREADY_KNOWN_RAW"
    elif fresh:
        outcome = "FINDING_PRODUCED"
    else:
        outcome = "NOVEL_RAW_NO_FINDING"
    step = SearchStep(
        depth=depth,
        gap_id=choice.gap.gap_id,
        dimension=choice.gap.dimension.value,
        capability=choice.capability,
        target=choice.target.canonical,
        raw_records=_raw_record_count(observation.payload),
        payload_fields=tuple(sorted(observation.payload)),
        returned_refs=returned_refs,
        new_refs=new_refs,
        normalized_findings=tuple(
            f"{finding.kind.value}:{finding.entity.canonical}" for finding in normalized.findings
        ),
        new_findings=tuple(f"{finding.kind.value}:{finding.entity.canonical}" for finding in fresh),
        hypotheses_before=len(case.hypotheses),
        hypotheses_after=len(case.hypotheses),
        hypotheses_changed=False,
        resolution_before=diagnosis.resolution.value,
        resolution_after=diagnosis.resolution.value,
        query_template=choice.query_template,
        observation_identity=choice.observation_identity,
        attempt_outcome=outcome,
    )
    return step, fresh, observation


def search_incident(
    source: ObservationSource,
    *,
    seed_policy: SeedPolicy | None = None,
    max_depth: int = 2,
    max_states: int = 256,
    diagnose: Callable[[Case], Diagnosis] = diagnose_case,
) -> MultiStepSearchResult:
    """Enumerate legal evidence-acquisition sequences without a model."""
    if max_depth < 1:
        raise ValueError("max_depth must be positive")
    bounded = initial_view(source, policy=seed_policy)
    initial_case = build_case(bounded)
    initial_diagnosis = diagnose(initial_case)
    backend = investigation_backend(source)
    tools = default_tools(backend)
    templates = query_templates(source)
    queue: deque[_State] = deque(
        [_State(initial_case, initial_diagnosis, (), (), frozenset(), (), 0)]
    )
    visited = {state_fingerprint(initial_case, initial_diagnosis, ())}
    rebuild_cache: dict[
        tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]], tuple[Case, Diagnosis]
    ] = {((), ()): (initial_case, initial_diagnosis)}
    attempts: list[SearchStep] = []
    solutions: list[SearchPath] = []
    if initial_diagnosis.resolution is Resolution.RESOLVED:
        solutions.append(SearchPath((), initial_diagnosis))
    explored = 0
    truncated = False
    while queue:
        state = queue.popleft()
        if state.depth >= max_depth:
            continue
        if explored >= max_states:
            truncated = True
            break
        explored += 1
        choices = legal_query_choices(state.diagnosis, tools, templates, state.query_keys)
        for choice in choices:
            step, fresh, observation = _step(
                state.depth + 1,
                state.case,
                state.diagnosis,
                choice,
                tools[choice.capability],
            )
            combined = deduplicate_findings((*state.findings, *fresh))
            coverage = (
                covered_frontier_dimensions(
                    state.diagnosis,
                    capability=choice.capability,
                    target=choice.target,
                )
                if observation is not None and observation.error is None
                else {}
            )
            next_queried = _merge_queried_dimensions(state.queried_dimensions, coverage)
            if fresh or coverage:
                rebuild_key = (
                    tuple(sorted(_finding_key(finding) for finding in combined)),
                    next_queried,
                )
                cached = rebuild_cache.get(rebuild_key)
                if cached is None:
                    cached = _rebuild_search_case(bounded, combined, next_queried, diagnose)
                    rebuild_cache[rebuild_key] = cached
                next_case, next_diagnosis = cached
            else:
                next_case, next_diagnosis = state.case, state.diagnosis
            step = replace(
                step,
                hypotheses_after=len(next_case.hypotheses),
                hypotheses_changed=tuple(
                    hypothesis.hypothesis_id for hypothesis in next_case.hypotheses
                )
                != tuple(hypothesis.hypothesis_id for hypothesis in state.case.hypotheses),
                resolution_after=next_diagnosis.resolution.value,
                frontier_changed=next_queried != state.queried_dimensions,
            )
            attempts.append(step)
            next_steps = (*state.steps, step)
            if next_diagnosis.resolution is Resolution.RESOLVED:
                solutions.append(SearchPath(next_steps, next_diagnosis))
                continue
            if len(next_steps) >= max_depth:
                continue
            fingerprint = state_fingerprint(next_case, next_diagnosis, next_queried)
            if fingerprint in visited:
                continue
            visited.add(fingerprint)
            queue.append(
                _State(
                    next_case,
                    next_diagnosis,
                    combined,
                    next_steps,
                    frozenset((*state.query_keys, choice.observation_identity)),
                    next_queried,
                    state.depth + 1,
                )
            )
    return MultiStepSearchResult(
        incident_id=source.incident_id(),
        seed_policy=(seed_policy or SeedPolicy()).name,
        initial_case=initial_case,
        initial_diagnosis=initial_diagnosis,
        attempts=tuple(attempts),
        solutions=tuple(solutions),
        max_depth=max_depth,
        explored_states=explored,
        truncated=truncated,
    )


__all__ = [
    "LegalQueryChoice",
    "MultiStepSearchResult",
    "query_templates",
    "legal_query_choices",
    "SearchPath",
    "SearchStep",
    "search_incident",
    "state_fingerprint",
]
