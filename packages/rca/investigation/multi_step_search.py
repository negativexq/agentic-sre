"""Deterministic bounded search over legal investigation queries.

This module measures the information ceiling of the active investigation
environment.  It uses the same backend, novelty helper, normalizers, case
builder, and resolver as the production graph; it never calls a policy or
reads benchmark labels.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any

from packages.rca.engine import Case, build_case, diagnose_case
from packages.rca.investigation.environment import (
    SeedPolicy,
    initial_view,
    investigation_backend,
)
from packages.rca.investigation.normalizers import (
    new_investigation_findings,
    normalize_observation,
)
from packages.rca.investigation.state import InvestigationTool
from packages.rca.investigation.tools import default_tools
from packages.rca.model import (
    Diagnosis,
    EntityRef,
    Finding,
    GapResolvability,
    Hypothesis,
    InformationGap,
    InvestigationQuery,
    Resolution,
)
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


@dataclass(frozen=True)
class _State:
    case: Case
    diagnosis: Diagnosis
    findings: tuple[Finding, ...]
    steps: tuple[SearchStep, ...]
    query_keys: frozenset[str]
    depth: int


def _query_for(source: ObservationSource) -> InvestigationQuery:
    return InvestigationQuery(end=source.observation_cutoff(), limit=64)


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
            "evidence": sorted(finding.evidence_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
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


def state_fingerprint(case: Case, diagnosis: Diagnosis) -> str:
    """Fingerprint only deterministic evidence and resolution state."""
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
    }
    return sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _query_key(capability: str, target: EntityRef, query: InvestigationQuery) -> str:
    """Treat equivalent semantic queries as the same branch action."""
    return json.dumps(
        {
            "capability": capability,
            "target": target.canonical,
            "query": query.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _resolvable_gaps(diagnosis: Diagnosis) -> tuple[InformationGap, ...]:
    return tuple(
        gap
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE and gap.candidate_tools
    )


def _queries(
    diagnosis: Diagnosis,
    tools: Mapping[str, InvestigationTool],
    query: InvestigationQuery,
    used: frozenset[str],
) -> tuple[tuple[InformationGap, str, EntityRef, str], ...]:
    choices: list[tuple[InformationGap, str, EntityRef, str]] = []
    for gap in _resolvable_gaps(diagnosis):
        for capability in sorted(gap.candidate_tools):
            if capability not in tools:
                continue
            for target in sorted(gap.entity_scope, key=lambda item: item.canonical):
                key = _query_key(capability, target, query)
                if key not in used:
                    choices.append((gap, capability, target, key))
    return tuple(choices)


def _step(
    depth: int,
    case: Case,
    diagnosis: Diagnosis,
    gap: InformationGap,
    capability: str,
    target: EntityRef,
    tool: InvestigationTool,
    query: InvestigationQuery,
) -> tuple[SearchStep, tuple[Finding, ...]]:
    execute_query = getattr(tool, "execute_query", None)
    observation = (
        execute_query(case, gap, target, query)
        if callable(execute_query)
        else tool.execute(case, gap, target)
    )
    normalized = normalize_observation(observation, case=case, gap=gap)
    fresh = new_investigation_findings(case.findings, normalized.findings)
    returned_refs = tuple(dict.fromkeys(observation.evidence_refs))
    known_refs = {evidence_id for finding in case.findings for evidence_id in finding.evidence_ids}
    new_refs = tuple(ref for ref in returned_refs if ref not in known_refs)
    step = SearchStep(
        depth=depth,
        gap_id=gap.gap_id,
        dimension=gap.dimension.value,
        capability=capability,
        target=target.canonical,
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
    )
    return step, fresh


def search_incident(
    source: ObservationSource,
    *,
    seed_policy: SeedPolicy | None = None,
    max_depth: int = 2,
    max_states: int = 256,
) -> MultiStepSearchResult:
    """Enumerate legal evidence-acquisition sequences without a model."""
    if max_depth < 1:
        raise ValueError("max_depth must be positive")
    bounded = initial_view(source, policy=seed_policy)
    initial_case = build_case(bounded)
    initial_diagnosis = diagnose_case(initial_case)
    backend = investigation_backend(source)
    tools = default_tools(backend)
    query = _query_for(source)
    queue: deque[_State] = deque([_State(initial_case, initial_diagnosis, (), (), frozenset(), 0)])
    visited = {state_fingerprint(initial_case, initial_diagnosis)}
    attempts: list[SearchStep] = []
    solutions: list[SearchPath] = []
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
        for gap, capability, target, key in _queries(
            state.diagnosis, tools, query, state.query_keys
        ):
            step, fresh = _step(
                state.depth + 1,
                state.case,
                state.diagnosis,
                gap,
                capability,
                target,
                tools[capability],
                query,
            )
            if not fresh:
                attempts.append(step)
                continue
            combined = tuple(new_investigation_findings(state.findings, fresh))
            next_case = build_case(bounded, extra_findings=combined)
            next_diagnosis = diagnose_case(next_case)
            step = replace(
                step,
                hypotheses_after=len(next_case.hypotheses),
                hypotheses_changed=tuple(
                    hypothesis.hypothesis_id for hypothesis in next_case.hypotheses
                )
                != tuple(hypothesis.hypothesis_id for hypothesis in state.case.hypotheses),
                resolution_after=next_diagnosis.resolution.value,
            )
            attempts.append(step)
            next_steps = (*state.steps, step)
            if next_diagnosis.resolution is Resolution.RESOLVED:
                solutions.append(SearchPath(next_steps, next_diagnosis))
                continue
            if len(next_steps) >= max_depth:
                continue
            fingerprint = state_fingerprint(next_case, next_diagnosis)
            if fingerprint in visited:
                continue
            visited.add(fingerprint)
            queue.append(
                _State(
                    next_case,
                    next_diagnosis,
                    combined,
                    next_steps,
                    frozenset((*state.query_keys, key)),
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
    "MultiStepSearchResult",
    "SearchPath",
    "SearchStep",
    "search_incident",
    "state_fingerprint",
]
