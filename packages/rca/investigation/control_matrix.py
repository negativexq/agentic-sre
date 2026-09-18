"""Deterministic four-way control and active-evidence ceiling diagnostics.

This module is intentionally an evaluation aid.  It does not call a policy,
read benchmark answers, or alter seed, ranking, normalization, or resolution
semantics.
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
    MultiStepSearchResult,
    _queries,
    query_templates,
    search_incident,
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
    FrontierStatus,
    GapDimension,
    InformationGap,
    InvestigationQuery,
    StructuralAlternative,
)
from packages.rca.source import ObservationSource


@dataclass(frozen=True)
class PromotionAudit:
    """One legal query measured through retrieval and RCA promotion."""

    gap_id: str
    dimension: str
    capability: str
    target: str
    raw_records: int
    returned_refs: tuple[str, ...]
    new_refs: tuple[str, ...]
    normalized_findings: tuple[str, ...]
    new_finding_keys: tuple[str, ...]
    finding_roles: tuple[str, ...]
    candidate_created: bool
    hypothesis_created_or_updated: bool
    hypothesis_ids_after: tuple[str, ...]
    plausibility_after: tuple[str, ...]
    resolution_before: str
    resolution_after: str
    classification: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "dimension": self.dimension,
            "capability": self.capability,
            "target": self.target,
            "raw_records": self.raw_records,
            "returned_refs": self.returned_refs,
            "new_refs": self.new_refs,
            "normalized_findings": self.normalized_findings,
            "new_finding_keys": self.new_finding_keys,
            "finding_roles": self.finding_roles,
            "candidate_created": self.candidate_created,
            "hypothesis_created_or_updated": self.hypothesis_created_or_updated,
            "hypothesis_ids_after": self.hypothesis_ids_after,
            "plausibility_after": self.plausibility_after,
            "resolution_before": self.resolution_before,
            "resolution_after": self.resolution_after,
            "classification": self.classification,
        }


@dataclass(frozen=True)
class FrontierMateriality:
    raw_alternatives: int
    material_alternatives: int
    observation_targets: int
    equivalence_groups: tuple[tuple[str, tuple[str, ...]], ...]
    lifecycle_counts: dict[str, int]


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
    frontier_materiality: FrontierMateriality
    search: MultiStepSearchResult

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "full": _diagnosis_summary(self.full_case, self.full_diagnosis),
            "seed": _diagnosis_summary(self.seed_case, self.seed_diagnosis),
            "exhaustive_active": _diagnosis_summary(
                self.exhaustive_case, self.exhaustive_diagnosis
            ),
            "active_findings": [_finding_key(item) for item in self.active_findings],
            "promotion_audits": [item.as_dict() for item in self.promotion_audits],
            "full_only_findings": self.full_only_findings,
            "active_only_findings": self.active_only_findings,
            "shared_findings": self.shared_findings,
            "full_only_reasons": self.full_only_reasons,
            "frontier_materiality": {
                "raw_alternatives": self.frontier_materiality.raw_alternatives,
                "material_alternatives": self.frontier_materiality.material_alternatives,
                "observation_targets": self.frontier_materiality.observation_targets,
                "equivalence_groups": self.frontier_materiality.equivalence_groups,
                "lifecycle_counts": self.frontier_materiality.lifecycle_counts,
            },
            "search": {
                "resolution": self.search.initial_diagnosis.resolution.value,
                "one_step": any(len(item.steps) == 1 for item in self.search.solutions),
                "two_step": any(len(item.steps) == 2 for item in self.search.solutions),
                "three_step": any(len(item.steps) == 3 for item in self.search.solutions),
            },
        }


def _finding_key(finding: Finding) -> str:
    return json.dumps(
        {
            "kind": finding.kind.value,
            "entity": finding.entity.canonical,
            "evidence": sorted(finding.evidence_ids),
            "role": finding.temporal_role.value,
            "at": finding.at.isoformat() if finding.at else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _finding_label(finding: Finding) -> str:
    return f"{finding.kind.value}:{finding.entity.canonical}:{','.join(finding.evidence_ids)}"


def _record_count(payload: Mapping[str, Any]) -> int:
    for key in ("versions", "events", "logs", "resource_pressure", "traffic"):
        records = payload.get(key)
        if isinstance(records, list):
            return len(records)
    return 0


def _hypothesis_ids(case: Case) -> tuple[str, ...]:
    return tuple(sorted(item.hypothesis_id for item in case.hypotheses))


def _plausible_ids(diagnosis: Diagnosis) -> tuple[str, ...]:
    trace = diagnosis.resolution_trace
    return tuple(sorted(trace.plausible_hypotheses if trace else ()))


def _diagnosis_summary(case: Case, diagnosis: Diagnosis) -> dict[str, Any]:
    return {
        "resolution": diagnosis.resolution.value,
        "investigation_status": diagnosis.investigation_status.value,
        "evidence_backed_hypotheses": len(case.hypotheses),
        "plausible_hypotheses": _plausible_ids(diagnosis),
        "leading_hypotheses": tuple(
            diagnosis.resolution_trace.leading_hypothesis_ids if diagnosis.resolution_trace else ()
        ),
        "leading_actor": diagnosis.root_cause.canonical if diagnosis.root_cause else None,
        "finding_count": len(case.findings),
        "finding_kinds": dict(sorted(Counter(item.kind.value for item in case.findings).items())),
        "structural_alternatives": len(case.structural_alternatives),
        "observation_targets": len(
            {target for item in case.structural_alternatives for target in item.observation_targets}
        ),
        "open_alternatives": sum(
            item.status is FrontierStatus.UNEXPLORED for item in case.structural_alternatives
        ),
    }


def _query_findings(
    case: Case,
    tools: Mapping[str, InvestigationTool],
    gap: InformationGap,
    capability: str,
    target: Any,
    query: InvestigationQuery,
) -> tuple[Any, tuple[Finding, ...], tuple[str, ...], tuple[str, ...]]:
    tool = tools[capability]
    execute_query = getattr(tool, "execute_query", None)
    observation = (
        execute_query(case, gap, target, query)
        if callable(execute_query)
        else tool.execute(case, gap, target)
    )
    returned_refs = tuple(dict.fromkeys(observation.evidence_refs))
    known_refs = {ref for finding in case.findings for ref in finding.evidence_ids}
    new_refs = tuple(ref for ref in returned_refs if ref not in known_refs)
    normalized = normalize_observation(observation, case=case, gap=gap)
    fresh = new_investigation_findings(case.findings, normalized.findings)
    return observation, fresh, new_refs, returned_refs


def _classify_promotion(
    *,
    raw_records: int,
    new_refs: tuple[str, ...],
    fresh: tuple[Finding, ...],
    seed_case: Case,
    after_case: Case,
    before: Diagnosis,
    after: Diagnosis,
) -> str:
    if not raw_records or not new_refs:
        return "NO_RAW_DATA"
    if not fresh:
        return "NOVEL_RAW_NO_FINDING"
    roles = {finding.temporal_role.value for finding in fresh}
    if roles and roles <= {"SUPPORTING"}:
        return "FINDING_SUPPORTING_ONLY"
    if roles and roles <= {"CONSEQUENCE"}:
        return "FINDING_CONSEQUENCE_ONLY"
    candidate_entities = {candidate.entity for candidate in after_case.candidates}
    if any(finding.entity not in candidate_entities for finding in fresh):
        return "CANDIDATE_NOT_CREATED"
    fresh_refs = {ref for finding in fresh for ref in finding.evidence_ids}
    affected = {
        hypothesis.hypothesis_id
        for hypothesis in after_case.hypotheses
        if any(ref in fresh_refs for finding in hypothesis.findings for ref in finding.evidence_ids)
    }
    if not affected:
        return "FINDING_NOT_CAUSALLY_LINKED"
    if _hypothesis_ids(seed_case) != _hypothesis_ids(after_case):
        if after.resolution is before.resolution:
            return "NO_RESOLUTION_EFFECT"
        return "NO_RESOLUTION_EFFECT"
    if roles and "INITIATING" not in roles:
        return "NO_ALIGNED_INITIATING_EVIDENCE"
    return "HYPOTHESIS_GROUPING_ABSORBED"


def _frontier_signature(item: StructuralAlternative) -> str:
    workload_targets = tuple(
        sorted(
            target.kind
            for target in item.observation_targets
            if target.kind
            in {
                "Deployment",
                "StatefulSet",
                "DaemonSet",
                "Job",
                "CronJob",
            }
        )
    )
    payload = {
        "role": item.role,
        "basis": tuple(sorted(item.structural_basis)),
        "path": tuple((hop.source.kind, hop.relation, hop.target.kind) for hop in item.causal_path),
        "dimensions": tuple(sorted(dimension.value for dimension in item.queryable_dimensions)),
        "workload_targets": workload_targets,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _frontier_materiality(alternatives: tuple[StructuralAlternative, ...]) -> FrontierMateriality:
    groups: dict[str, list[str]] = defaultdict(list)
    for item in alternatives:
        groups[_frontier_signature(item)].append(item.actor.canonical)
    equivalence_groups = tuple(
        sorted((signature, tuple(sorted(actors))) for signature, actors in groups.items())
    )
    lifecycle = Counter(item.status.value for item in alternatives)
    return FrontierMateriality(
        raw_alternatives=len(alternatives),
        material_alternatives=len(equivalence_groups),
        observation_targets=len(
            {target for item in alternatives for target in item.observation_targets}
        ),
        equivalence_groups=equivalence_groups,
        lifecycle_counts=dict(sorted(lifecycle.items())),
    )


def _full_only_reason(
    finding: Finding,
    audits: tuple[PromotionAudit, ...],
    alternatives: tuple[StructuralAlternative, ...],
) -> str:
    evidence = set(finding.evidence_ids)
    if any(evidence.intersection(item.new_refs) for item in audits):
        return "NORMALIZATION_GAP"
    if any(
        finding.entity == target for item in alternatives for target in item.observation_targets
    ):
        return "QUERY_DIMENSION_MISSING"
    if any(finding.entity == item.actor for item in alternatives):
        return "RAW_RECORD_NOT_RETURNED"
    return "TARGET_NOT_IN_FRONTIER"


def run_control_matrix(
    source: ObservationSource,
    *,
    seed_policy: SeedPolicy | None = None,
    max_depth: int = 3,
    max_states: int = 256,
) -> ControlMatrixResult:
    """Run full, seed, exhaustive-active, and bounded-search controls."""
    full_case = build_case(source)
    full_diagnosis = diagnose_case(full_case)
    bounded = initial_view(source, policy=seed_policy)
    seed_case = build_case(bounded)
    seed_diagnosis = diagnose_case(seed_case)
    backend = investigation_backend(source)
    tools = default_tools(backend)
    templates = query_templates(source)
    queries = _queries(seed_diagnosis, tools, templates, frozenset())

    union: dict[str, Finding] = {}
    audits: list[PromotionAudit] = []
    for gap, capability, target, _key, _label, query in queries:
        observation, fresh, new_refs, returned_refs = _query_findings(
            seed_case, tools, gap, capability, target, query
        )
        for finding in fresh:
            union[_finding_key(finding)] = finding
        after_case = build_case(bounded, extra_findings=fresh) if fresh else seed_case
        after = diagnose_case(after_case)
        audits.append(
            PromotionAudit(
                gap_id=gap.gap_id,
                dimension=gap.dimension.value,
                capability=capability,
                target=target.canonical,
                raw_records=_record_count(observation.payload),
                returned_refs=returned_refs,
                new_refs=new_refs,
                normalized_findings=tuple(_finding_label(item) for item in fresh),
                new_finding_keys=tuple(_finding_key(item) for item in fresh),
                finding_roles=tuple(sorted({item.temporal_role.value for item in fresh})),
                candidate_created=any(
                    item.entity in {c.entity for c in after_case.candidates} for item in fresh
                ),
                hypothesis_created_or_updated=_hypothesis_ids(seed_case)
                != _hypothesis_ids(after_case),
                hypothesis_ids_after=_hypothesis_ids(after_case),
                plausibility_after=_plausible_ids(after),
                resolution_before=seed_diagnosis.resolution.value,
                resolution_after=after.resolution.value,
                classification=_classify_promotion(
                    raw_records=_record_count(observation.payload),
                    new_refs=new_refs,
                    fresh=fresh,
                    seed_case=seed_case,
                    after_case=after_case,
                    before=seed_diagnosis,
                    after=after,
                ),
            )
        )
    active_findings = tuple(union.values())
    exhaustive_case = build_case(bounded, extra_findings=active_findings)
    exhaustive_diagnosis = diagnose_case(exhaustive_case)

    full_keys = {_finding_key(item): item for item in full_case.findings}
    active_keys = {_finding_key(item): item for item in exhaustive_case.findings}
    shared = tuple(sorted(set(full_keys) & set(active_keys)))
    full_only = tuple(sorted(set(full_keys) - set(active_keys)))
    active_only = tuple(sorted(set(active_keys) - set(full_keys)))
    full_only_reasons = {
        key: _full_only_reason(
            full_keys[key], tuple(audits), tuple(seed_case.structural_alternatives)
        )
        for key in full_only
    }

    search = search_incident(
        source,
        seed_policy=seed_policy,
        max_depth=max_depth,
        max_states=max_states,
    )
    queried_dimensions: dict[str, set[GapDimension]] = defaultdict(set)
    for audit in audits:
        for alternative_id, dimensions in covered_frontier_dimensions(
            seed_diagnosis,
            capability=audit.capability,
            target=EntityRef.parse(audit.target),
        ).items():
            queried_dimensions[alternative_id].update(dimensions)
    audited_alternatives = apply_frontier_progress(
        tuple(seed_case.structural_alternatives),
        hypotheses=exhaustive_case.hypotheses,
        queried_dimensions_by_alternative={
            alternative_id: tuple(sorted(dimensions, key=lambda item: item.value))
            for alternative_id, dimensions in queried_dimensions.items()
        },
    )
    return ControlMatrixResult(
        incident_id=source.incident_id(),
        full_case=full_case,
        full_diagnosis=full_diagnosis,
        seed_case=seed_case,
        seed_diagnosis=seed_diagnosis,
        exhaustive_case=exhaustive_case,
        exhaustive_diagnosis=exhaustive_diagnosis,
        active_findings=active_findings,
        promotion_audits=tuple(audits),
        full_only_findings=full_only,
        active_only_findings=active_only,
        shared_findings=shared,
        full_only_reasons=full_only_reasons,
        frontier_materiality=_frontier_materiality(audited_alternatives),
        search=search,
    )


def aggregate_classifications(results: tuple[ControlMatrixResult, ...]) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                audit.classification for result in results for audit in result.promotion_audits
            ).items()
        )
    )


__all__ = [
    "ControlMatrixResult",
    "FrontierMateriality",
    "PromotionAudit",
    "aggregate_classifications",
    "run_control_matrix",
]
