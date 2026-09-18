"""One-step investigation opportunity audit.

This is a developer diagnostic, not a planner and not a benchmark grader.  It
enumerates every currently legal gap/capability/target query and sends its raw
result through the same novelty, normalization, case rebuild, and resolver
path used by the bounded graph.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from packages.rca.engine import Case, build_case, diagnose_case
from packages.rca.investigation.environment import (
    InitialObservationView,
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
    GapResolvability,
    InformationGap,
    InvestigationQuery,
    Resolution,
)
from packages.rca.source import ObservationSource


@dataclass(frozen=True)
class QueryOpportunity:
    gap_id: str
    dimension: str
    capability: str
    target: str
    raw_records: int
    returned_refs: tuple[str, ...]
    new_raw_refs: tuple[str, ...]
    already_known_refs: tuple[str, ...]
    normalized_findings: tuple[str, ...]
    new_findings: tuple[str, ...]
    hypotheses_before: tuple[str, ...]
    hypotheses_after: tuple[str, ...]
    gaps_before: tuple[str, ...]
    gaps_after: tuple[str, ...]
    resolution_before: str
    resolution_after: str
    effect: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IncidentOpportunityAudit:
    incident_id: str
    initial_resolution: str
    initial_hypotheses: tuple[str, ...]
    initial_resolvable_gaps: tuple[str, ...]
    classification: str
    opportunities: tuple[QueryOpportunity, ...]
    seed_policy: str = "latest-only-plus-5m-events"
    initial_access: dict[str, tuple[str, ...]] | None = None
    hidden_access_counts: dict[str, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["opportunities"] = [item.as_dict() for item in self.opportunities]
        return payload


def _hypothesis_fingerprint(case: Case) -> tuple[str, ...]:
    return tuple(
        sorted(
            f"{hypothesis.hypothesis_id}|{hypothesis.causal_actor.canonical}|"
            f"{','.join(sorted(finding.kind.value for finding in hypothesis.findings))}"
            for hypothesis in case.hypotheses
        )
    )


def _gap_fingerprint(diagnosis: Diagnosis) -> tuple[str, ...]:
    return tuple(
        sorted(
            f"{gap.gap_id}|{gap.resolvability.value}|{','.join(sorted(gap.candidate_tools))}"
            for gap in diagnosis.information_gaps
            if gap.resolvability is GapResolvability.RESOLVABLE and gap.candidate_tools
        )
    )


def _query_for(source: ObservationSource) -> InvestigationQuery:
    # The audit measures the available environment ceiling.  It uses a generic
    # bounded query ending at the source cutoff, never a scenario-specific
    # target/window or any evaluator answer.
    return InvestigationQuery(end=source.observation_cutoff(), limit=64)


def _full_access(source: ObservationSource) -> dict[str, set[str]]:
    history = source.object_history()
    pods = tuple(entity for entity in history if entity.kind == "Pod")
    return {
        "initial_history_refs": {
            version.evidence_id for versions in history.values() for version in versions
        },
        "initial_event_refs": {event.evidence_id for event in source.events()},
        "initial_log_refs": {record.evidence_id for record in source.error_logs()},
        "initial_metric_refs": {
            item.evidence_id
            for item in source.resource_pressure(pods, datetime.min.replace(tzinfo=UTC))
        },
        "initial_traffic_refs": {item.evidence_id for item in source.traffic_observations()},
    }


def _hidden_access_counts(
    initial: Mapping[str, Sequence[str]], full: Mapping[str, set[str]]
) -> dict[str, int]:
    return {
        key.replace("initial_", "hidden_"): len(full.get(key, set()) - set(initial.get(key, ())))
        for key in full
    }


def _raw_record_count(payload: Mapping[str, Any]) -> int:
    for key in ("versions", "events", "logs", "resource_pressure", "traffic"):
        records = payload.get(key)
        if isinstance(records, list):
            return len(records)
    return 0


def _execute_opportunity(
    source: ObservationSource,
    initial_source: ObservationSource,
    initial_case: Case,
    initial_diagnosis: Diagnosis,
    tools: Mapping[str, InvestigationTool],
    gap: InformationGap,
    capability: str,
    target: EntityRef,
) -> QueryOpportunity:
    query = _query_for(source)
    tool = tools[capability]
    execute_query = getattr(tool, "execute_query", None)
    if not callable(execute_query):
        observation = tool.execute(initial_case, gap, target)
    else:
        observation = execute_query(initial_case, gap, target, query)
    returned_refs = tuple(dict.fromkeys(observation.evidence_refs))
    known_refs = {
        evidence_id for finding in initial_case.findings for evidence_id in finding.evidence_ids
    }
    new_refs = tuple(ref for ref in returned_refs if ref not in known_refs)
    already_known = tuple(ref for ref in returned_refs if ref in known_refs)
    normalized = normalize_observation(observation, case=initial_case, gap=gap)
    fresh = new_investigation_findings(initial_case.findings, normalized.findings)
    if fresh:
        rebuilt = build_case(initial_source, extra_findings=fresh)
        after = diagnose_case(rebuilt)
    else:
        rebuilt = initial_case
        after = initial_diagnosis
    before_hypotheses = _hypothesis_fingerprint(initial_case)
    after_hypotheses = _hypothesis_fingerprint(rebuilt)
    before_gaps = _gap_fingerprint(initial_diagnosis)
    after_gaps = _gap_fingerprint(after)
    resolution_changed = after.resolution is not initial_diagnosis.resolution
    hypotheses_changed = after_hypotheses != before_hypotheses
    gaps_changed = after_gaps != before_gaps
    if resolution_changed and after.resolution is Resolution.RESOLVED:
        effect = "RESOLUTION_CHANGED"
    elif hypotheses_changed:
        effect = "HYPOTHESES_CHANGED"
    elif gaps_changed:
        effect = "GAPS_CHANGED"
    elif fresh:
        effect = "NEW_FINDING_NO_HYPOTHESIS_CHANGE"
    elif new_refs:
        effect = "NEW_RAW_NO_FINDING"
    else:
        effect = "NONE"
    return QueryOpportunity(
        gap_id=gap.gap_id,
        dimension=gap.dimension.value,
        capability=capability,
        target=target.canonical,
        raw_records=_raw_record_count(observation.payload),
        returned_refs=returned_refs,
        new_raw_refs=new_refs,
        already_known_refs=already_known,
        normalized_findings=tuple(
            f"{finding.kind.value}:{finding.entity.canonical}" for finding in normalized.findings
        ),
        new_findings=tuple(f"{finding.kind.value}:{finding.entity.canonical}" for finding in fresh),
        hypotheses_before=before_hypotheses,
        hypotheses_after=after_hypotheses,
        gaps_before=before_gaps,
        gaps_after=after_gaps,
        resolution_before=initial_diagnosis.resolution.value,
        resolution_after=after.resolution.value,
        effect=effect,
    )


def _classify(
    has_hypotheses: bool,
    gaps: Sequence[InformationGap],
    opportunities: Sequence[QueryOpportunity],
) -> str:
    if not has_hypotheses:
        return "NO_HYPOTHESES"
    if not gaps:
        return "NO_RESOLVABLE_GAP"
    if any(item.effect == "RESOLUTION_CHANGED" for item in opportunities):
        return "RESOLVABLE_BY_AVAILABLE_QUERY"
    if not any(item.new_raw_refs for item in opportunities):
        return "NO_NOVEL_RAW_TELEMETRY"
    if not any(item.new_findings for item in opportunities):
        return "NORMALIZATION_GAP"
    if not any(item.hypotheses_after != item.hypotheses_before for item in opportunities):
        return "NO_HYPOTHESIS_EFFECT"
    return "NO_RESOLUTION_EFFECT"


def audit_incident(
    source: ObservationSource,
    *,
    seed_policy: SeedPolicy | None = None,
    include_full_access: bool = False,
) -> IncidentOpportunityAudit:
    """Audit all legal one-step queries from a bounded initial diagnosis."""
    bounded = initial_view(source, policy=seed_policy)
    initial_case = build_case(bounded)
    diagnosis = diagnose_case(initial_case)
    gaps = tuple(
        gap
        for gap in diagnosis.information_gaps
        if gap.resolvability is GapResolvability.RESOLVABLE and gap.candidate_tools
    )
    backend = investigation_backend(source)
    tools = default_tools(backend)
    opportunities: list[QueryOpportunity] = []
    for gap in gaps:
        for capability in sorted(gap.candidate_tools):
            if capability not in tools:
                continue
            for target in sorted(gap.entity_scope, key=lambda item: item.canonical):
                opportunities.append(
                    _execute_opportunity(
                        source,
                        bounded,
                        initial_case,
                        diagnosis,
                        tools,
                        gap,
                        capability,
                        target,
                    )
                )
    access: dict[str, tuple[str, ...]]
    if isinstance(bounded, InitialObservationView):
        access = bounded.access_ledger()
    else:
        access = {key: tuple() for key in _full_access(source)}
    full = _full_access(source) if include_full_access else {}
    return IncidentOpportunityAudit(
        incident_id=source.incident_id(),
        initial_resolution=diagnosis.resolution.value,
        initial_hypotheses=tuple(
            hypothesis.hypothesis_id for hypothesis in initial_case.hypotheses
        ),
        initial_resolvable_gaps=tuple(gap.gap_id for gap in gaps),
        classification=_classify(bool(initial_case.hypotheses), gaps, opportunities),
        opportunities=tuple(opportunities),
        seed_policy=(seed_policy or SeedPolicy()).name,
        initial_access=access,
        hidden_access_counts=_hidden_access_counts(access, full) if include_full_access else None,
    )


__all__ = ["IncidentOpportunityAudit", "QueryOpportunity", "audit_incident"]
