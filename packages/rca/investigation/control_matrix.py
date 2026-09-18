"""Deterministic full/seed/active control and fidelity diagnostics.

This module is measurement code.  It does not call a policy, read benchmark
answers, or change RCA semantics.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from packages.rca.engine import Case, build_case, diagnose_case
from packages.rca.frontier import apply_frontier_progress, covered_frontier_dimensions
from packages.rca.investigation.environment import SeedPolicy, initial_view, investigation_backend
from packages.rca.investigation.multi_step_search import (
    LegalQueryChoice,
    MultiStepSearchResult,
    legal_query_choices,
    query_templates,
    search_incident,
)
from packages.rca.investigation.normalizers import (
    deduplicate_findings,
    new_investigation_findings,
    normalize_observation,
)
from packages.rca.investigation.state import InvestigationTool
from packages.rca.investigation.tools import default_tools, make_observation
from packages.rca.model import (
    Diagnosis,
    EntityRef,
    Finding,
    FindingKind,
    FrontierStatus,
    GapDimension,
    InvestigationObservation,
    Resolution,
    StructuralAlternative,
)
from packages.rca.source import ObservationSource

_ACQUISITION_DETAIL_KEYS = frozenset(
    {"observation_id", "investigation_gap_id", "investigation_capability"}
)


@dataclass(frozen=True)
class PromotionAudit:
    """One legal query measured through retrieval and post-build promotion."""

    gap_id: str
    dimension: str
    capability: str
    target: str
    alternative_ids: tuple[str, ...]
    query_template: str
    observation_identity: str
    raw_records: int
    returned_refs: tuple[str, ...]
    new_refs: tuple[str, ...]
    already_seen_refs: tuple[str, ...]
    normalized_finding_core_keys: tuple[str, ...]
    new_finding_core_keys: tuple[str, ...]
    post_rebuild_finding_keys: tuple[str, ...]
    post_rebuild_roles: tuple[str, ...]
    candidate_created: bool
    hypothesis_linked: bool
    resolution_before: str
    resolution_after: str
    attempt_outcome: str
    promotion_classification: str | None
    audit_defect: str | None = None
    # Diagnostic-only epistemic snapshots.  These are captured around the
    # existing rebuild; they do not participate in investigation execution.
    episode_states_before: tuple[tuple[str, str], ...] = ()
    episode_states_after: tuple[tuple[str, str], ...] = ()
    observation_outcome: str = "UNKNOWN"

    def as_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "dimension": self.dimension,
            "capability": self.capability,
            "target": self.target,
            "alternative_ids": self.alternative_ids,
            "query_template": self.query_template,
            "observation_identity": self.observation_identity,
            "raw_records": self.raw_records,
            "returned_refs": self.returned_refs,
            "new_refs": self.new_refs,
            "already_seen_refs": self.already_seen_refs,
            "normalized_finding_core_keys": self.normalized_finding_core_keys,
            "new_finding_core_keys": self.new_finding_core_keys,
            "post_rebuild_finding_keys": self.post_rebuild_finding_keys,
            "post_rebuild_roles": self.post_rebuild_roles,
            "candidate_created": self.candidate_created,
            "hypothesis_linked": self.hypothesis_linked,
            "resolution_before": self.resolution_before,
            "resolution_after": self.resolution_after,
            "attempt_outcome": self.attempt_outcome,
            "promotion_classification": self.promotion_classification,
            "audit_defect": self.audit_defect,
            "episode_states_before": self.episode_states_before,
            "episode_states_after": self.episode_states_after,
            "observation_outcome": self.observation_outcome,
        }


@dataclass(frozen=True)
class PromotionGroupingGap:
    finding_key: str
    full_candidate_entities: tuple[str, ...]
    active_candidate_entities: tuple[str, ...]
    full_hypothesis_episodes: tuple[str, ...]
    active_hypothesis_episodes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding_key": self.finding_key,
            "full_candidate_entities": self.full_candidate_entities,
            "active_candidate_entities": self.active_candidate_entities,
            "full_hypothesis_episodes": self.full_hypothesis_episodes,
            "active_hypothesis_episodes": self.active_hypothesis_episodes,
        }


@dataclass(frozen=True)
class FrontierStateSummary:
    raw_alternatives: int
    structural_shape_groups: tuple[tuple[str, tuple[str, ...]], ...]
    observation_targets: int
    unexplored: int
    partial: int
    queried_no_finding: int
    promoted: int

    @property
    def shape_groups(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return self.structural_shape_groups


@dataclass(frozen=True)
class ExhaustiveActiveResult:
    case: Case
    diagnosis: Diagnosis
    accumulated_findings: tuple[Finding, ...]
    audits: tuple[PromotionAudit, ...]
    attempted_observations: tuple[str, ...]
    queried_dimensions: tuple[tuple[str, tuple[str, ...]], ...]
    rounds: int
    truncated: bool
    termination_reason: str
    unique_returned_refs: tuple[str, ...]
    unique_new_refs: tuple[str, ...]
    unique_refs_that_produced_findings: tuple[str, ...]

    @property
    def unique_new_refs_with_no_finding(self) -> tuple[str, ...]:
        return tuple(
            sorted(set(self.unique_new_refs) - set(self.unique_refs_that_produced_findings))
        )


@dataclass(frozen=True)
class ControlMatrixResult:
    incident_id: str
    full_case: Case
    full_diagnosis: Diagnosis
    seed_case: Case
    seed_diagnosis: Diagnosis
    exhaustive_case: Case
    exhaustive_diagnosis: Diagnosis
    active_findings: tuple[Finding, ...]
    promotion_audits: tuple[PromotionAudit, ...]
    full_only_findings: tuple[str, ...]
    active_only_findings: tuple[str, ...]
    shared_findings: tuple[str, ...]
    full_only_reasons: dict[str, str]
    frontier_state: FrontierStateSummary
    search: MultiStepSearchResult
    exhaustive: ExhaustiveActiveResult
    full_semantic_findings: tuple[str, ...]
    active_semantic_findings: tuple[str, ...]
    shared_semantic_findings: tuple[str, ...]
    full_only_semantic_findings: tuple[str, ...]
    active_only_semantic_findings: tuple[str, ...]
    full_refs: tuple[str, ...]
    seed_refs: tuple[str, ...]
    active_accessible_refs: tuple[str, ...]
    shared_refs: tuple[str, ...]
    missing_full_refs: tuple[str, ...]
    active_fidelity: str
    full_control_resolvability: str
    planning_opportunity: str
    planning_reason: str
    promotion_grouping_gaps: tuple[PromotionGroupingGap, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "full": _diagnosis_summary(self.full_case, self.full_diagnosis),
            "seed": _diagnosis_summary(self.seed_case, self.seed_diagnosis),
            "exhaustive_active": _diagnosis_summary(
                self.exhaustive_case, self.exhaustive_diagnosis
            ),
            "d1": self.search.depth_status(1),
            "d2": self.search.depth_status(2),
            "d3": self.search.depth_status(3),
            "search_truncated": self.search.truncated,
            "truncation_depth": self.search.truncation_depth,
            "investigation_status": self.exhaustive_diagnosis.investigation_status.value,
            "semantic_finding_coverage": {
                "full": self.full_semantic_findings,
                "shared": self.shared_semantic_findings,
                "active": self.active_semantic_findings,
                "full_only": self.full_only_semantic_findings,
                "active_only": self.active_only_semantic_findings,
            },
            "raw_evidence_coverage": {
                "full": self.full_refs,
                "seed": self.seed_refs,
                "active_accessible": self.active_accessible_refs,
                "shared": self.shared_refs,
                "missing": self.missing_full_refs,
            },
            "active_fidelity": self.active_fidelity,
            "full_control_resolvability": self.full_control_resolvability,
            "planning_opportunity": self.planning_opportunity,
            "planning_reason": self.planning_reason,
            "full_only_reasons": self.full_only_reasons,
            "promotion_grouping_gaps": [item.as_dict() for item in self.promotion_grouping_gaps],
            "promotion_audits": [item.as_dict() for item in self.promotion_audits],
            "frontier": {
                "raw_alternatives": self.frontier_state.raw_alternatives,
                "structural_shape_groups": self.frontier_state.structural_shape_groups,
                "observation_targets": self.frontier_state.observation_targets,
                "unexplored": self.frontier_state.unexplored,
                "partial": self.frontier_state.partial,
                "queried_no_finding": self.frontier_state.queried_no_finding,
                "promoted": self.frontier_state.promoted,
            },
            "closure": {
                "rounds": self.exhaustive.rounds,
                "unique_observation_identities": len(self.exhaustive.attempted_observations),
                "truncated": self.exhaustive.truncated,
                "termination_reason": self.exhaustive.termination_reason,
                "unique_returned_refs": self.exhaustive.unique_returned_refs,
                "unique_new_refs": self.exhaustive.unique_new_refs,
                "unique_refs_that_produced_findings": self.exhaustive.unique_refs_that_produced_findings,
                "unique_new_refs_with_no_finding": self.exhaustive.unique_new_refs_with_no_finding,
            },
        }


def _diagnosis_summary(case: Case, diagnosis: Diagnosis) -> dict[str, Any]:
    trace = diagnosis.resolution_trace
    return {
        "resolution": diagnosis.resolution.value,
        "investigation_status": diagnosis.investigation_status.value,
        "evidence_backed_hypotheses": len(case.hypotheses),
        "plausible_hypotheses": tuple(sorted(trace.plausible_hypotheses if trace else ())),
        "leading_hypotheses": tuple(sorted(trace.leading_hypothesis_ids if trace else ())),
        "leading_actor": diagnosis.root_cause.canonical if diagnosis.root_cause else None,
        "finding_count": len(case.findings),
        "finding_kinds": dict(sorted(Counter(item.kind.value for item in case.findings).items())),
        "structural_alternatives": len(case.structural_alternatives),
        "observation_targets": len(
            {target for item in case.structural_alternatives for target in item.observation_targets}
        ),
    }


def _episode_states(case: Case, diagnosis: Diagnosis) -> tuple[tuple[str, str], ...]:
    """Return actor/state pairs for diagnostics without changing runtime semantics."""
    trace = diagnosis.resolution_trace
    if trace is None:
        return ()
    by_id = {item.hypothesis_id: item for item in case.hypotheses}
    states = {
        **{item: "SUPPORTED" for item in trace.plausible_hypotheses},
        **{item: "UNRESOLVED" for item in trace.unresolved_hypotheses},
        **{item: "CONTRADICTED" for item in trace.eliminated_hypotheses},
    }
    values = tuple(
        sorted(
            (
                by_id[hypothesis_id].causal_actor.canonical,
                states.get(hypothesis_id, "UNKNOWN"),
            )
            for hypothesis_id in states
            if hypothesis_id in by_id
        )
    )
    return values


def _diagnostic_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _diagnostic_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {
            str(key): _diagnostic_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item) for item in value]
    return value


def _semantic_details(finding: Finding) -> dict[str, Any]:
    return {
        key: value for key, value in finding.details.items() if key not in _ACQUISITION_DETAIL_KEYS
    }


def semantic_finding_core_key(finding: Finding) -> str:
    """Diagnostic semantic key before temporal role annotation."""
    return json.dumps(
        {
            "kind": finding.kind.value,
            "entity": finding.entity.canonical,
            "at": finding.at.isoformat() if finding.at else None,
            "summary": finding.summary,
            "related": tuple(sorted(item.canonical for item in finding.related)),
            "details": _diagnostic_value(_semantic_details(finding)),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def semantic_finding_key(finding: Finding) -> str:
    """Diagnostic post-build key; raw provenance and onset annotations excluded."""
    return json.dumps(
        {
            "core": semantic_finding_core_key(finding),
            "temporal_role": finding.temporal_role.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _record_count(payload: Mapping[str, Any]) -> int:
    for key in ("versions", "events", "logs", "resource_pressure", "traffic"):
        records = payload.get(key)
        if isinstance(records, list):
            return len(records)
    return 0


def _plausible_ids(diagnosis: Diagnosis) -> tuple[str, ...]:
    trace = diagnosis.resolution_trace
    return tuple(sorted(trace.plausible_hypotheses if trace else ()))


def _frontier_signature(item: StructuralAlternative) -> str:
    payload = {
        "role": item.role,
        "basis": tuple(sorted(item.structural_basis)),
        "path": tuple((hop.source.kind, hop.relation, hop.target.kind) for hop in item.causal_path),
        "dimensions": tuple(sorted(dimension.value for dimension in item.queryable_dimensions)),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _frontier_state(alternatives: tuple[StructuralAlternative, ...]) -> FrontierStateSummary:
    groups: dict[str, list[str]] = defaultdict(list)
    for item in alternatives:
        groups[_frontier_signature(item)].append(item.actor.canonical)
    shapes = tuple(
        sorted((signature, tuple(sorted(actors))) for signature, actors in groups.items())
    )
    return FrontierStateSummary(
        raw_alternatives=len(alternatives),
        structural_shape_groups=shapes,
        observation_targets=len(
            {target for item in alternatives for target in item.observation_targets}
        ),
        unexplored=sum(
            item.status is FrontierStatus.UNEXPLORED and not item.queried_dimensions
            for item in alternatives
        ),
        partial=sum(
            item.status is FrontierStatus.UNEXPLORED and bool(item.queried_dimensions)
            for item in alternatives
        ),
        queried_no_finding=sum(
            item.status is FrontierStatus.QUERIED_NO_CAUSAL_FINDING for item in alternatives
        ),
        promoted=sum(item.status is FrontierStatus.PROMOTED for item in alternatives),
    )


def _apply_frontier(
    case: Case,
    queried_dimensions: tuple[tuple[str, tuple[str, ...]], ...],
) -> Case:
    decoded = {
        alternative_id: tuple(GapDimension(value) for value in values)
        for alternative_id, values in queried_dimensions
    }
    case.structural_alternatives = list(
        apply_frontier_progress(
            tuple(case.structural_alternatives),
            hypotheses=case.hypotheses,
            queried_dimensions_by_alternative=decoded,
        )
    )
    return case


def _merge_dimensions(
    current: Mapping[str, set[str]], additions: Mapping[str, tuple[Any, ...]]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    merged = {key: set(values) for key, values in current.items()}
    for alternative_id, dimensions in additions.items():
        merged.setdefault(alternative_id, set()).update(
            dimension.value if hasattr(dimension, "value") else str(dimension)
            for dimension in dimensions
        )
    return tuple(sorted((key, tuple(sorted(values))) for key, values in merged.items()))


def _rebuild(
    bounded: ObservationSource,
    findings: tuple[Finding, ...],
    queried_dimensions: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[Case, Diagnosis]:
    case = _apply_frontier(build_case(bounded, extra_findings=findings), queried_dimensions)
    return case, diagnose_case(case)


def _execute(
    choice: LegalQueryChoice, case: Case, tools: Mapping[str, InvestigationTool]
) -> InvestigationObservation:
    tool = tools[choice.capability]
    execute_query = getattr(tool, "execute_query", None)
    try:
        if callable(execute_query):
            result = execute_query(case, choice.gap, choice.target, choice.query)
            if not isinstance(result, InvestigationObservation):
                raise TypeError("investigation tool returned an invalid observation")
            return result
        return tool.execute(case, choice.gap, choice.target)
    except Exception as error:
        return make_observation(
            gap=choice.gap,
            capability=choice.capability,
            target=choice.target,
            payload={},
            source_class="tool_error",
            error=f"{type(error).__name__}: {error}",
        )


def _outcome(
    observation: InvestigationObservation,
    raw_records: int,
    new_refs: tuple[str, ...],
    fresh: tuple[Finding, ...],
) -> str:
    if observation.error:
        return "TOOL_ERROR"
    if raw_records == 0:
        return "NO_RAW_RECORDS"
    if fresh:
        return "FINDING_PRODUCED"
    if not new_refs:
        return "ALREADY_KNOWN_RAW"
    return "NOVEL_RAW_NO_FINDING"


def _audit_outcome(
    observation: InvestigationObservation,
    raw_records: int,
    returned_refs: tuple[str, ...],
    new_refs: tuple[str, ...],
    fresh: tuple[Finding, ...],
) -> tuple[str, str | None]:
    if raw_records and not returned_refs:
        return "AUDIT_DEFECT", "UNREFERENCED_RAW_RECORDS"
    return _outcome(observation, raw_records, new_refs, fresh), None


def _post_rebuild_matches(findings: tuple[Finding, ...], case: Case) -> tuple[Finding, ...]:
    cores = {semantic_finding_core_key(finding) for finding in findings}
    return tuple(
        finding for finding in case.findings if semantic_finding_core_key(finding) in cores
    )


def _promotion_classification(
    fresh: tuple[Finding, ...],
    after_case: Case,
    before: Diagnosis,
    after: Diagnosis,
) -> tuple[str | None, tuple[Finding, ...]]:
    if not fresh:
        return None, ()
    matched = _post_rebuild_matches(fresh, after_case)
    candidate_keys = {
        semantic_finding_core_key(finding)
        for candidate in after_case.candidates
        for finding in candidate.findings
    }
    hypothesis_keys = {
        semantic_finding_core_key(finding)
        for hypothesis in after_case.hypotheses
        for finding in hypothesis.findings
    }
    matched_keys = {semantic_finding_core_key(finding) for finding in matched}
    if not matched_keys.intersection(candidate_keys):
        return "CANDIDATE_NOT_CREATED", matched
    if not matched_keys.intersection(hypothesis_keys):
        return "FINDING_NOT_CAUSALLY_LINKED", matched
    roles = {finding.temporal_role.value for finding in matched}
    if roles and roles <= {"SUPPORTING"}:
        return "FINDING_SUPPORTING_ONLY", matched
    if roles and roles <= {"CONSEQUENCE"}:
        return "FINDING_CONSEQUENCE_ONLY", matched
    if "INITIATING" not in roles:
        return "NO_ALIGNED_INITIATING_EVIDENCE", matched
    if before.resolution is not after.resolution:
        return "RESOLUTION_EFFECT", matched
    return "NO_RESOLUTION_EFFECT", matched


@dataclass(frozen=True)
class _PendingAudit:
    choice: LegalQueryChoice
    observation: InvestigationObservation
    fresh: tuple[Finding, ...]
    returned_refs: tuple[str, ...]
    new_refs: tuple[str, ...]
    already_seen_refs: tuple[str, ...]
    normalized_core_keys: tuple[str, ...]
    outcome: str
    audit_defect: str | None
    resolution_before: str
    episode_states_before: tuple[tuple[str, str], ...]


def exhaustive_active_closure(
    source: ObservationSource,
    *,
    seed_policy: SeedPolicy | None = None,
    max_rounds: int = 16,
    max_observations: int = 4096,
) -> ExhaustiveActiveResult:
    """Execute every currently legal unseen observation to a fixed point."""
    bounded = initial_view(source, policy=seed_policy)
    current_case = build_case(bounded)
    current_diagnosis = diagnose_case(current_case)
    backend = investigation_backend(source)
    tools = default_tools(backend)
    templates = query_templates(source)
    accumulated: tuple[Finding, ...] = ()
    attempted: set[str] = set()
    queried: dict[str, set[str]] = {}
    seen_refs = {ref for finding in current_case.findings for ref in finding.evidence_ids}
    returned_refs: set[str] = set()
    new_refs_global: set[str] = set()
    finding_refs: set[str] = set()
    audits: list[PromotionAudit] = []
    rounds = 0
    truncated = False
    termination_reason = "FIXED_POINT"

    while True:
        choices = legal_query_choices(current_diagnosis, tools, templates, attempted)
        if not choices:
            termination_reason = "FIXED_POINT"
            break
        if rounds >= max_rounds:
            truncated = True
            termination_reason = "MAX_ROUNDS"
            break
        pending: list[_PendingAudit] = []
        round_fresh: list[Finding] = []
        round_dimensions: dict[str, tuple[Any, ...]] = {}
        hit_observation_limit = False
        for choice in choices:
            if len(attempted) >= max_observations:
                hit_observation_limit = True
                break
            attempted.add(choice.observation_identity)
            observation = _execute(choice, current_case, tools)
            refs = tuple(dict.fromkeys(observation.evidence_refs))
            returned_refs.update(refs)
            novel = tuple(ref for ref in refs if ref not in seen_refs)
            already = tuple(ref for ref in refs if ref in seen_refs)
            seen_refs.update(refs)
            new_refs_global.update(novel)
            normalized = normalize_observation(observation, case=current_case, gap=choice.gap)
            fresh = new_investigation_findings(
                (*current_case.findings, *round_fresh), normalized.findings
            )
            round_fresh.extend(fresh)
            finding_refs.update(ref for finding in fresh for ref in finding.evidence_ids)
            coverage = (
                covered_frontier_dimensions(
                    current_diagnosis,
                    capability=choice.capability,
                    target=choice.target,
                )
                if observation.error is None
                else {}
            )
            for alternative_id, dimensions in coverage.items():
                round_dimensions[alternative_id] = tuple(
                    sorted(
                        {
                            *round_dimensions.get(alternative_id, ()),
                            *dimensions,
                        },
                        key=lambda item: item.value,
                    )
                )
            raw_records = _record_count(observation.payload)
            outcome, defect = _audit_outcome(observation, raw_records, refs, novel, fresh)
            pending.append(
                _PendingAudit(
                    choice=choice,
                    observation=observation,
                    fresh=tuple(fresh),
                    returned_refs=refs,
                    new_refs=novel,
                    already_seen_refs=already,
                    normalized_core_keys=tuple(
                        sorted(semantic_finding_core_key(item) for item in normalized.findings)
                    ),
                    outcome=outcome,
                    audit_defect=defect,
                    resolution_before=current_diagnosis.resolution.value,
                    episode_states_before=_episode_states(current_case, current_diagnosis),
                )
            )
        combined = tuple(
            new_investigation_findings(current_case.findings, deduplicate_findings(round_fresh))
        )
        accumulated = deduplicate_findings((*accumulated, *combined))
        queried_state = _merge_dimensions(queried, round_dimensions)
        queried = {key: set(values) for key, values in queried_state}
        next_case, next_diagnosis = _rebuild(bounded, accumulated, queried_state)
        for item in pending:
            classification, matched = _promotion_classification(
                item.fresh, next_case, current_diagnosis, next_diagnosis
            )
            audits.append(
                PromotionAudit(
                    gap_id=item.choice.gap.gap_id,
                    dimension=item.choice.gap.dimension.value,
                    capability=item.choice.capability,
                    target=item.choice.target.canonical,
                    alternative_ids=item.choice.alternative_ids,
                    query_template=item.choice.query_template,
                    observation_identity=item.choice.observation_identity,
                    raw_records=_record_count(item.observation.payload),
                    returned_refs=item.returned_refs,
                    new_refs=item.new_refs,
                    already_seen_refs=item.already_seen_refs,
                    normalized_finding_core_keys=item.normalized_core_keys,
                    new_finding_core_keys=tuple(
                        sorted(semantic_finding_core_key(finding) for finding in item.fresh)
                    ),
                    post_rebuild_finding_keys=tuple(
                        sorted(semantic_finding_key(finding) for finding in matched)
                    ),
                    post_rebuild_roles=tuple(
                        sorted({finding.temporal_role.value for finding in matched})
                    ),
                    candidate_created=bool(
                        {
                            semantic_finding_core_key(finding)
                            for candidate in next_case.candidates
                            for finding in candidate.findings
                        }.intersection({semantic_finding_core_key(finding) for finding in matched})
                    ),
                    hypothesis_linked=bool(
                        {
                            semantic_finding_core_key(finding)
                            for hypothesis in next_case.hypotheses
                            for finding in hypothesis.findings
                        }.intersection({semantic_finding_core_key(finding) for finding in matched})
                    ),
                    resolution_before=item.resolution_before,
                    resolution_after=next_diagnosis.resolution.value,
                    attempt_outcome=item.outcome,
                    promotion_classification=classification,
                    audit_defect=item.audit_defect,
                    episode_states_before=item.episode_states_before,
                    episode_states_after=_episode_states(next_case, next_diagnosis),
                    observation_outcome=item.observation.outcome.value,
                )
            )
        current_case, current_diagnosis = next_case, next_diagnosis
        rounds += 1
        if hit_observation_limit:
            truncated = True
            termination_reason = "MAX_OBSERVATIONS"
            break
    return ExhaustiveActiveResult(
        case=current_case,
        diagnosis=current_diagnosis,
        accumulated_findings=accumulated,
        audits=tuple(audits),
        attempted_observations=tuple(sorted(attempted)),
        queried_dimensions=tuple(
            sorted((key, tuple(sorted(values))) for key, values in queried.items())
        ),
        rounds=rounds,
        truncated=truncated,
        termination_reason=termination_reason,
        unique_returned_refs=tuple(sorted(returned_refs)),
        unique_new_refs=tuple(sorted(new_refs_global)),
        unique_refs_that_produced_findings=tuple(sorted(finding_refs)),
    )


_NORMALIZER_FINDING_KINDS_BY_CAPABILITY: dict[str, frozenset[FindingKind]] = {
    "history": frozenset(
        {
            FindingKind.OBJECT_CREATED,
            FindingKind.OBJECT_DELETED,
            FindingKind.CONFIG_CHANGE,
            FindingKind.SPEC_CHANGE,
            FindingKind.IMAGE_CHANGE,
            FindingKind.SCALE_CHANGE,
            FindingKind.ROLLOUT_RESTART,
        }
    ),
    "events": frozenset({FindingKind.FAILURE_EVENT, FindingKind.AUTOSCALING_FAILURE}),
    "logs": frozenset({FindingKind.DEPENDENCY_ERRORS}),
    "resource_pressure": frozenset({FindingKind.RESOURCE_PRESSURE}),
    "traffic": frozenset({FindingKind.TRAFFIC_INCREASE}),
}


def _capabilities_for_finding_kind(kind: FindingKind) -> tuple[str, ...]:
    return tuple(
        sorted(
            capability
            for capability, kinds in _NORMALIZER_FINDING_KINDS_BY_CAPABILITY.items()
            if kind in kinds
        )
    )


def _full_only_reason(
    finding: Finding,
    *,
    active_refs: set[str],
    audits: tuple[PromotionAudit, ...],
    tools: Mapping[str, InvestigationTool],
) -> str:
    required_refs = set(finding.evidence_ids)
    if not required_refs:
        return "UNCLASSIFIED_MECHANICALLY"
    if required_refs <= active_refs:
        return "RAW_RETURNED_NORMALIZATION_GAP"
    capabilities = _capabilities_for_finding_kind(finding.kind)
    if not capabilities or not any(capability in tools for capability in capabilities):
        return "CAPABILITY_NOT_EXPOSED"
    targets = {finding.entity, *finding.related}
    authorized = [
        audit
        for audit in audits
        if audit.capability in capabilities and EntityRef.parse(audit.target) in targets
    ]
    if not authorized:
        return "TARGET_NOT_AUTHORIZED"
    return "RAW_RECORD_NOT_RETURNED" if required_refs - active_refs else "UNCLASSIFIED_MECHANICALLY"


def _full_control_state(resolution: Resolution) -> str:
    return {
        Resolution.RESOLVED: "FULL_RESOLVED",
        Resolution.AMBIGUOUS: "FULL_AMBIGUOUS",
        Resolution.INSUFFICIENT_EVIDENCE: "FULL_INSUFFICIENT",
    }[resolution]


def _planning_state(
    full: Resolution, active: Resolution, search: MultiStepSearchResult
) -> tuple[str, str]:
    if full is Resolution.RESOLVED and active is not Resolution.RESOLVED:
        return "NO_PLANNING_CONCLUSION", "ACTIVE_FIDELITY_PREVENTS_COMPARISON"
    if full is Resolution.AMBIGUOUS or full is Resolution.INSUFFICIENT_EVIDENCE:
        if active is Resolution.RESOLVED:
            return "ACTIVE_SEMANTIC_DIVERGENCE", "ACTIVE_SEMANTIC_DIVERGENCE"
        return (
            "NO_PLANNING_CONCLUSION",
            "FULL_CONTROL_AMBIGUOUS"
            if full is Resolution.AMBIGUOUS
            else "FULL_CONTROL_INSUFFICIENT",
        )
    status = search.depth_status(3)
    if status == "RESOLVED":
        return "DETERMINISTIC_PLAN_EXISTS", "DETERMINISTIC_PLAN_FOUND"
    if status == "NOT_RESOLVED":
        return "PLANNING_OPPORTUNITY", "NO_PLAN_WITHIN_COMPLETE_D3"
    return "NO_PLANNING_CONCLUSION", "BOUNDED_SEARCH_TRUNCATED"


def _finding_promotion_effect(
    case: Case, finding_core_key: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    candidate_entities = tuple(
        sorted(
            {
                candidate.entity.canonical
                for candidate in case.candidates
                if any(
                    semantic_finding_core_key(finding) == finding_core_key
                    for finding in candidate.findings
                )
            }
        )
    )
    episodes = {
        json.dumps(
            {
                "causal_actor": hypothesis.causal_actor.canonical,
                "members": sorted(entity.canonical for entity in hypothesis.members),
                "manifestations": sorted(entity.canonical for entity in hypothesis.manifestations),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for hypothesis in case.hypotheses
        if any(
            semantic_finding_core_key(finding) == finding_core_key
            for finding in hypothesis.findings
        )
    }
    return candidate_entities, tuple(sorted(episodes))


def run_control_matrix(
    source: ObservationSource,
    *,
    seed_policy: SeedPolicy | None = None,
    max_depth: int = 3,
    max_states: int = 256,
    max_rounds: int = 16,
    max_observations: int = 4096,
) -> ControlMatrixResult:
    """Run full, seed, fixed-point active, and bounded-search controls."""
    full_case = build_case(source)
    full_diagnosis = diagnose_case(full_case)
    bounded = initial_view(source, policy=seed_policy)
    seed_case = build_case(bounded)
    seed_diagnosis = diagnose_case(seed_case)
    exhaustive = exhaustive_active_closure(
        source,
        seed_policy=seed_policy,
        max_rounds=max_rounds,
        max_observations=max_observations,
    )
    exhaustive_case = exhaustive.case
    exhaustive_diagnosis = exhaustive.diagnosis
    backend = investigation_backend(source)
    tools = default_tools(backend)
    full_map = {semantic_finding_key(item): item for item in full_case.findings}
    active_map = {semantic_finding_key(item): item for item in exhaustive_case.findings}
    shared = tuple(sorted(set(full_map) & set(active_map)))
    full_only = tuple(sorted(set(full_map) - set(active_map)))
    active_only = tuple(sorted(set(active_map) - set(full_map)))
    seed_refs = {ref for finding in seed_case.findings for ref in finding.evidence_ids}
    full_refs = {ref for finding in full_case.findings for ref in finding.evidence_ids}
    active_refs = seed_refs | set(exhaustive.unique_returned_refs)
    shared_refs = full_refs & active_refs
    missing_refs = full_refs - active_refs
    full_only_reasons = {
        key: _full_only_reason(
            full_map[key], active_refs=active_refs, audits=exhaustive.audits, tools=tools
        )
        for key in full_only
    }
    queried = {
        alternative_id: tuple(GapDimension(value) for value in values)
        for alternative_id, values in exhaustive.queried_dimensions
    }
    final_alternatives = apply_frontier_progress(
        tuple(exhaustive_case.structural_alternatives),
        hypotheses=exhaustive_case.hypotheses,
        queried_dimensions_by_alternative=queried,
    )
    grouping_gaps = tuple(
        PromotionGroupingGap(
            finding_key=key,
            full_candidate_entities=full_effect[0],
            active_candidate_entities=active_effect[0],
            full_hypothesis_episodes=full_effect[1],
            active_hypothesis_episodes=active_effect[1],
        )
        for key in shared
        for full_finding in (full_map[key],)
        for core_key in (semantic_finding_core_key(full_finding),)
        for full_effect, active_effect in (
            (
                _finding_promotion_effect(full_case, core_key),
                _finding_promotion_effect(exhaustive_case, core_key),
            ),
        )
        if full_effect != active_effect
    )
    active_fidelity = (
        "ACTIVE_PARITY"
        if not full_only and not active_only and not missing_refs
        else "ACTIVE_FIDELITY_GAP"
    )
    search = search_incident(
        source,
        seed_policy=seed_policy,
        max_depth=max_depth,
        max_states=max_states,
    )
    planning_opportunity, planning_reason = _planning_state(
        full_diagnosis.resolution, exhaustive_diagnosis.resolution, search
    )
    return ControlMatrixResult(
        incident_id=source.incident_id(),
        full_case=full_case,
        full_diagnosis=full_diagnosis,
        seed_case=seed_case,
        seed_diagnosis=seed_diagnosis,
        exhaustive_case=exhaustive_case,
        exhaustive_diagnosis=exhaustive_diagnosis,
        active_findings=exhaustive.accumulated_findings,
        promotion_audits=exhaustive.audits,
        full_only_findings=full_only,
        active_only_findings=active_only,
        shared_findings=shared,
        full_only_reasons=full_only_reasons,
        frontier_state=_frontier_state(final_alternatives),
        search=search,
        exhaustive=exhaustive,
        full_semantic_findings=tuple(sorted(full_map)),
        active_semantic_findings=tuple(sorted(active_map)),
        shared_semantic_findings=shared,
        full_only_semantic_findings=full_only,
        active_only_semantic_findings=active_only,
        full_refs=tuple(sorted(full_refs)),
        seed_refs=tuple(sorted(seed_refs)),
        active_accessible_refs=tuple(sorted(active_refs)),
        shared_refs=tuple(sorted(shared_refs)),
        missing_full_refs=tuple(sorted(missing_refs)),
        active_fidelity=active_fidelity,
        full_control_resolvability=_full_control_state(full_diagnosis.resolution),
        planning_opportunity=planning_opportunity,
        planning_reason=planning_reason,
        promotion_grouping_gaps=grouping_gaps,
    )


def aggregate_classifications(results: tuple[ControlMatrixResult, ...]) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                audit.promotion_classification
                for result in results
                for audit in result.promotion_audits
                if audit.promotion_classification is not None
            ).items()
        )
    )


__all__ = [
    "ControlMatrixResult",
    "ExhaustiveActiveResult",
    "FrontierStateSummary",
    "PromotionAudit",
    "PromotionGroupingGap",
    "aggregate_classifications",
    "exhaustive_active_closure",
    "run_control_matrix",
    "semantic_finding_core_key",
    "semantic_finding_key",
    "_capabilities_for_finding_kind",
    "_finding_promotion_effect",
    "_full_only_reason",
    "_planning_state",
]
