"""Ground-truth-separated diagnostics for deterministic causal semantics.

This module observes the production RCA objects.  It does not participate in
candidate generation, hypothesis grouping, verification, or resolution.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast

from packages.rca.engine import Case, build_case, diagnose_case
from packages.rca.model import (
    Candidate,
    Diagnosis,
    EvidenceTemporalRole,
    Finding,
    Hypothesis,
)
from packages.rca.resolution import (
    _evidence_key,
    _has_aligned_initiating,
    dominates,
    hypothesis_signature,
)
from packages.rca.source import ObservationSource

_ACQUISITION_KEYS = frozenset(
    {"observation_id", "investigation_gap_id", "investigation_capability"}
)
_CHANGE_KINDS = frozenset(
    {"CONFIG_CHANGE", "SPEC_CHANGE", "IMAGE_CHANGE", "SCALE_CHANGE", "ROLLOUT_RESTART"}
)
_MECHANISM_ORDER = (
    "SIGNATURE_COLLISION",
    "ACTOR_PROJECTION_COLLISION",
    "EPISODE_SPLIT_OR_OVERLAP",
    "DOMINANCE_MODEL_GAP",
    "UNUSED_DIAGNOSTIC_SIGNAL_PRESENT",
    "NO_MEASURED_DISCRIMINATOR",
)
_SIGNAL_ORDER = (
    "EXACT_SIGNATURE_COLLISION",
    "ACTOR_PROJECTION_RELATION",
    "EPISODE_OVERLAP",
    "SEMANTIC_EVIDENCE_SUPERSET_WITHOUT_DOMINANCE",
    "EXACT_TEMPORAL_ORDER_AVAILABLE",
    "VERIFICATION_DECISION_DIFFERENCE",
    "VERIFICATION_PREDICATE_DIFFERENCE",
    "CAUSAL_PATH_SHAPE_DIFFERENCE",
    "CAUSAL_PATH_LENGTH_DIFFERENCE",
    "PROVENANCE_CLASS_DIFFERENCE",
    "DISJOINT_PLAUSIBLE_CAUSES",
)


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _finding_core(finding: Finding) -> str:
    details = {key: value for key, value in finding.details.items() if key not in _ACQUISITION_KEYS}
    return json.dumps(
        {
            "kind": finding.kind.value,
            "entity": finding.entity.canonical,
            "at": finding.at.isoformat() if finding.at else None,
            "summary": finding.summary,
            "related": sorted(entity.canonical for entity in finding.related),
            "details": _json_value(details),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def finding_key(finding: Finding) -> str:
    """Diagnostic Finding identity: semantics plus temporal role, no provenance."""
    return json.dumps(
        {"core": json.loads(_finding_core(finding)), "temporal_role": finding.temporal_role.value},
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class VerificationPredicateSnapshot:
    name: str
    status: str
    evidence_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class CandidateSnapshot:
    entity: str
    score: float
    finding_keys: tuple[str, ...]
    initiating_finding_keys: tuple[str, ...]
    supporting_finding_keys: tuple[str, ...]
    consequence_finding_keys: tuple[str, ...]
    causal_path: tuple[tuple[str, str, str], ...]
    causal_explanation: str
    linked_symptoms: tuple[str, ...]


@dataclass(frozen=True)
class HypothesisSnapshot:
    hypothesis_id: str
    causal_actor: str
    members: tuple[str, ...]
    manifestations: tuple[str, ...]
    score: float
    finding_keys: tuple[str, ...]
    initiating_finding_keys: tuple[str, ...]
    supporting_finding_keys: tuple[str, ...]
    contradictory_finding_keys: tuple[str, ...]
    initiating_kinds: tuple[str, ...]
    supporting_kinds: tuple[str, ...]
    contradiction_kinds: tuple[str, ...]
    earliest_initiating_delta_seconds: float | None
    latest_initiating_delta_seconds: float | None
    causal_paths: tuple[tuple[tuple[str, str, str], ...], ...]
    causal_explanation: str
    linked_symptoms: tuple[str, ...]
    signature: dict[str, object]
    plausible: bool
    plausibility_reasons: tuple[str, ...]
    verification_decision: str | None
    verification_predicates: tuple[VerificationPredicateSnapshot, ...]
    is_leading: bool


@dataclass(frozen=True)
class HypothesisCompetition:
    left_hypothesis_id: str
    right_hypothesis_id: str
    both_plausible: bool
    both_leading: bool
    exact_signature_equal: bool
    left_dominates_right: bool
    right_dominates_left: bool
    actor_projection_relation: bool
    shared_member_entities: tuple[str, ...]
    shared_manifestation_entities: tuple[str, ...]
    shared_semantic_findings: tuple[str, ...]
    shared_evidence_refs: tuple[str, ...]
    left_only_initiating_kinds: tuple[str, ...]
    right_only_initiating_kinds: tuple[str, ...]
    left_earliest_initiating_delta: float | None
    right_earliest_initiating_delta: float | None
    initiating_delta_difference_seconds: float | None
    causal_path_shapes_equal: bool
    symptom_relation_equal: bool
    provenance_classes_equal: bool
    verification_decision_equal: bool
    verification_predicate_differences: tuple[str, ...]
    signature_information_losses: tuple[str, ...]
    diagnostic_signals: tuple[str, ...]
    primary_mechanism: str
    left_to_right_dominance_blockers: tuple[str, ...]
    right_to_left_dominance_blockers: tuple[str, ...]


@dataclass(frozen=True)
class CausalSemanticsAudit:
    incident_id: str
    resolution: str
    finding_count: int
    finding_entities: tuple[str, ...]
    candidate_count: int
    hypothesis_count: int
    plausible_hypothesis_ids: tuple[str, ...]
    leading_hypothesis_ids: tuple[str, ...]
    eliminated_hypothesis_ids: tuple[str, ...]
    hypothesis_snapshots: tuple[HypothesisSnapshot, ...]
    candidate_snapshots: tuple[CandidateSnapshot, ...]
    competitions: tuple[HypothesisCompetition, ...]
    ambiguity_mechanisms: tuple[str, ...]
    primary_ambiguity_mechanism: str | None
    resolution_decision_basis: str
    unresolved_dimensions: tuple[str, ...]
    elimination_records: tuple[dict[str, object], ...]
    stage_funnel: dict[str, int]
    information_inventory: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(asdict(self)))


def _path_shape(path: Iterable[Any]) -> tuple[tuple[str, str, str], ...]:
    # The production topology owns path selection.  The audit keeps its
    # diagnostic shape stable when equivalent service/dependency hops are
    # selected in a different order by the interpreter.
    relation_aliases = {
        "backs": "service_route",
        "serves": "service_route",
        "called_by": "dependency",
        "dependency_of": "dependency",
    }
    workload_kinds = {"Pod", "Deployment", "StatefulSet", "DaemonSet"}
    return tuple(
        (
            "WORKLOAD" if hop.source.kind in workload_kinds else hop.source.kind,
            relation_aliases.get(hop.relation, hop.relation),
            "WORKLOAD" if hop.target.kind in workload_kinds else hop.target.kind,
        )
        for hop in path
    )


def _paths(hypothesis: Hypothesis) -> tuple[tuple[tuple[str, str, str], ...], ...]:
    return tuple(sorted({_path_shape(path) for path in hypothesis.causal_paths if path}))


def _deltas(findings: Iterable[Finding]) -> tuple[float, ...]:
    return tuple(
        finding.onset_delta_seconds
        for finding in findings
        if finding.temporal_role is EvidenceTemporalRole.INITIATING
        and finding.onset_delta_seconds is not None
    )


def _verification_snapshot(trace: Any) -> tuple[VerificationPredicateSnapshot, ...]:
    if trace is None:
        return ()
    return tuple(
        VerificationPredicateSnapshot(
            name=item.name,
            status=item.status.value,
            evidence_ids=tuple(item.evidence_ids),
            detail=item.detail,
        )
        for item in trace.predicates
    )


def _candidate_snapshot(candidate: Candidate) -> CandidateSnapshot:
    # Candidate path selection is a production ranking detail and can choose
    # equivalent routes across interpreter hash seeds.  The detailed path
    # diagnostics come from the hypothesis pair audit; candidate snapshots use
    # the stable fact that the candidate is linked to an observed symptom.
    stable_path = (("ACTOR", "causal_link", "SYMPTOM"),) if candidate.linked_symptoms else ()
    return CandidateSnapshot(
        entity=candidate.entity.canonical,
        score=candidate.score,
        finding_keys=tuple(sorted(finding_key(item) for item in candidate.findings)),
        initiating_finding_keys=tuple(
            sorted(
                finding_key(item)
                for item in candidate.findings
                if item.temporal_role.value == "INITIATING"
            )
        ),
        supporting_finding_keys=tuple(
            sorted(
                finding_key(item)
                for item in candidate.findings
                if item.temporal_role.value == "SUPPORTING"
            )
        ),
        consequence_finding_keys=tuple(
            sorted(
                finding_key(item)
                for item in candidate.findings
                if item.temporal_role.value == "CONSEQUENCE"
            )
        ),
        causal_path=stable_path,
        causal_explanation=("CAUSAL_LINK" if candidate.linked_symptoms else "UNLINKED"),
        linked_symptoms=tuple(sorted(candidate.linked_symptoms)),
    )


def _hypothesis_snapshot(
    hypothesis: Hypothesis,
    *,
    plausible: bool,
    reasons: tuple[str, ...],
    verification: Any,
    leading: bool,
) -> HypothesisSnapshot:
    deltas = _deltas(hypothesis.findings)
    signature = hypothesis_signature(hypothesis)
    return HypothesisSnapshot(
        hypothesis_id=hypothesis.hypothesis_id,
        causal_actor=hypothesis.causal_actor.canonical,
        members=tuple(sorted(item.canonical for item in hypothesis.members)),
        manifestations=tuple(sorted(item.canonical for item in hypothesis.manifestations)),
        score=hypothesis.score,
        finding_keys=tuple(sorted(finding_key(item) for item in hypothesis.findings)),
        initiating_finding_keys=tuple(
            sorted(finding_key(item) for item in hypothesis.initiating_findings)
        ),
        supporting_finding_keys=tuple(
            sorted(finding_key(item) for item in hypothesis.supporting_findings)
        ),
        contradictory_finding_keys=tuple(
            sorted(finding_key(item) for item in hypothesis.contradictory_findings)
        ),
        initiating_kinds=tuple(sorted(item.kind.value for item in hypothesis.initiating_findings)),
        supporting_kinds=tuple(sorted(item.kind.value for item in hypothesis.supporting_findings)),
        contradiction_kinds=tuple(
            sorted(item.kind.value for item in hypothesis.contradictory_findings)
        ),
        earliest_initiating_delta_seconds=min(deltas) if deltas else None,
        latest_initiating_delta_seconds=max(deltas) if deltas else None,
        causal_paths=_paths(hypothesis),
        causal_explanation=hypothesis.causal_explanation,
        linked_symptoms=tuple(sorted(hypothesis.linked_symptoms)),
        signature=_json_value(signature.model_dump(mode="json")),
        plausible=plausible,
        plausibility_reasons=reasons,
        verification_decision=verification.decision.value
        if verification and verification.decision
        else None,
        verification_predicates=_verification_snapshot(verification),
        is_leading=leading,
    )


def _projection_relation(left: Hypothesis, right: Hypothesis) -> bool:
    if left.causal_actor in (*right.members, *right.manifestations):
        return True
    if right.causal_actor in (*left.members, *left.manifestations):
        return True
    shared = set(left.members) & set(right.members)
    return bool(shared and any(item.entity in shared for item in (*left.findings, *right.findings)))


def _dominance_blockers(stronger: Hypothesis, weaker: Hypothesis) -> tuple[str, ...]:
    if dominates(stronger, weaker):
        return ()
    blockers: list[str] = []
    if stronger.contradictory_findings:
        blockers.append("STRONGER_HAS_CONTRADICTION")
    if not _has_aligned_initiating(stronger):
        blockers.append("STRONGER_LACKS_ALIGNED_INITIATING")
    stronger_keys = {_evidence_key(item) for item in stronger.findings}
    weaker_keys = {_evidence_key(item) for item in weaker.findings}
    if not weaker_keys <= stronger_keys:
        blockers.append("WEAKER_EVIDENCE_SHAPE_NOT_SUBSET")
    stronger_initiating = {
        _evidence_key(item)
        for item in stronger.initiating_findings
        if item.temporal_role is EvidenceTemporalRole.INITIATING
    }
    weaker_initiating = {
        _evidence_key(item)
        for item in weaker.initiating_findings
        if item.temporal_role is EvidenceTemporalRole.INITIATING
    }
    if not stronger_initiating > weaker_initiating:
        blockers.append("NO_ADDITIONAL_INITIATING_EVIDENCE_SHAPE")
    return tuple(blockers)


def _competition(
    left: Hypothesis, right: Hypothesis, audits: dict[str, Any]
) -> HypothesisCompetition:
    left_sig, right_sig = hypothesis_signature(left), hypothesis_signature(right)
    left_keys, right_keys = (
        {finding_key(item) for item in left.findings},
        {finding_key(item) for item in right.findings},
    )
    left_refs = {ref for item in left.findings for ref in item.evidence_ids}
    right_refs = {ref for item in right.findings for ref in item.evidence_ids}
    left_deltas, right_deltas = _deltas(left.findings), _deltas(right.findings)
    left_verification, right_verification = (
        audits.get(left.hypothesis_id),
        audits.get(right.hypothesis_id),
    )
    left_pred = (
        {item.name: item.status.value for item in left_verification.predicates}
        if left_verification
        else {}
    )
    right_pred = (
        {item.name: item.status.value for item in right_verification.predicates}
        if right_verification
        else {}
    )
    pred_differences = tuple(
        sorted(
            f"{name}: {left_pred.get(name)} vs {right_pred.get(name)}"
            for name in set(left_pred) | set(right_pred)
            if left_pred.get(name) != right_pred.get(name)
        )
    )
    signals: set[str] = set()
    exact_signature_equal = left_sig == right_sig
    projection = _projection_relation(left, right)
    shared_members = tuple(
        sorted(item.canonical for item in set(left.members) & set(right.members))
    )
    shared_manifestations = tuple(
        sorted(item.canonical for item in set(left.manifestations) & set(right.manifestations))
    )
    shared_findings = tuple(sorted(left_keys & right_keys))
    shared_refs = tuple(sorted(left_refs & right_refs))
    if exact_signature_equal:
        signals.add("EXACT_SIGNATURE_COLLISION")
    if projection:
        signals.add("ACTOR_PROJECTION_RELATION")
    if shared_findings or shared_refs or shared_members or shared_manifestations:
        signals.add("EPISODE_OVERLAP")
    if (left_keys > right_keys or right_keys > left_keys) and not (
        dominates(left, right) or dominates(right, left)
    ):
        signals.add("SEMANTIC_EVIDENCE_SUPERSET_WITHOUT_DOMINANCE")
    left_earliest = min(left_deltas) if left_deltas else None
    right_earliest = min(right_deltas) if right_deltas else None
    delta_difference = (
        abs(left_earliest - right_earliest)
        if left_earliest is not None and right_earliest is not None
        else None
    )
    if left_earliest is not None and right_earliest is not None and left_earliest != right_earliest:
        signals.add("EXACT_TEMPORAL_ORDER_AVAILABLE")
    if (
        left_verification
        and right_verification
        and left_verification.decision != right_verification.decision
    ):
        signals.add("VERIFICATION_DECISION_DIFFERENCE")
    if pred_differences:
        signals.add("VERIFICATION_PREDICATE_DIFFERENCE")
    if left_sig.causal_path_shape != right_sig.causal_path_shape:
        signals.add("CAUSAL_PATH_SHAPE_DIFFERENCE")
    left_hops = min((len(path) for path in left.causal_paths), default=0)
    right_hops = min((len(path) for path in right.causal_paths), default=0)
    if left_hops != right_hops:
        signals.add("CAUSAL_PATH_LENGTH_DIFFERENCE")
    if left_sig.provenance_classes != right_sig.provenance_classes:
        signals.add("PROVENANCE_CLASS_DIFFERENCE")
    if not (
        shared_findings or shared_refs or projection or shared_members or shared_manifestations
    ):
        signals.add("DISJOINT_PLAUSIBLE_CAUSES")
    signature_information_losses: set[str] = set()
    if left.causal_actor != right.causal_actor:
        signature_information_losses.add("exact_actor_identity")
    if set(left.members) != set(right.members):
        signature_information_losses.add("member_identity")
    if left_earliest != right_earliest and left_sig.temporal_profile == right_sig.temporal_profile:
        signature_information_losses.add("exact_onset_delta")
    if left_keys != right_keys and left_sig == right_sig:
        signature_information_losses.add("exact_finding_details")
    if left_refs != right_refs and left_sig.provenance_classes == right_sig.provenance_classes:
        signature_information_losses.add("specific_evidence_provenance")
    if pred_differences:
        signature_information_losses.add("verification_predicates")
    if (
        left.causal_paths != right.causal_paths
        and left_sig.causal_path_shape == right_sig.causal_path_shape
    ):
        signature_information_losses.add("exact_path_entities")
    if "EXACT_SIGNATURE_COLLISION" in signals:
        mechanism = "SIGNATURE_COLLISION"
    elif projection:
        mechanism = "ACTOR_PROJECTION_COLLISION"
    elif shared_findings or shared_refs or shared_members or shared_manifestations:
        mechanism = "EPISODE_SPLIT_OR_OVERLAP"
    elif "SEMANTIC_EVIDENCE_SUPERSET_WITHOUT_DOMINANCE" in signals:
        mechanism = "DOMINANCE_MODEL_GAP"
    elif signals & {
        "EXACT_TEMPORAL_ORDER_AVAILABLE",
        "VERIFICATION_DECISION_DIFFERENCE",
        "VERIFICATION_PREDICATE_DIFFERENCE",
        "CAUSAL_PATH_SHAPE_DIFFERENCE",
        "CAUSAL_PATH_LENGTH_DIFFERENCE",
        "PROVENANCE_CLASS_DIFFERENCE",
    }:
        mechanism = "UNUSED_DIAGNOSTIC_SIGNAL_PRESENT"
    else:
        mechanism = "NO_MEASURED_DISCRIMINATOR"
    return HypothesisCompetition(
        left_hypothesis_id=left.hypothesis_id,
        right_hypothesis_id=right.hypothesis_id,
        both_plausible=True,
        both_leading=False,
        exact_signature_equal=exact_signature_equal,
        left_dominates_right=dominates(left, right),
        right_dominates_left=dominates(right, left),
        actor_projection_relation=projection,
        shared_member_entities=shared_members,
        shared_manifestation_entities=shared_manifestations,
        shared_semantic_findings=shared_findings,
        shared_evidence_refs=shared_refs,
        left_only_initiating_kinds=tuple(
            sorted(set(left_sig.initiating_kinds) - set(right_sig.initiating_kinds))
        ),
        right_only_initiating_kinds=tuple(
            sorted(set(right_sig.initiating_kinds) - set(left_sig.initiating_kinds))
        ),
        left_earliest_initiating_delta=left_earliest,
        right_earliest_initiating_delta=right_earliest,
        initiating_delta_difference_seconds=delta_difference,
        causal_path_shapes_equal=left_sig.causal_path_shape == right_sig.causal_path_shape,
        symptom_relation_equal=left_sig.symptom_relation == right_sig.symptom_relation,
        provenance_classes_equal=left_sig.provenance_classes == right_sig.provenance_classes,
        verification_decision_equal=bool(
            left_verification
            and right_verification
            and left_verification.decision == right_verification.decision
        ),
        verification_predicate_differences=pred_differences,
        signature_information_losses=tuple(sorted(signature_information_losses)),
        diagnostic_signals=tuple(signal for signal in _SIGNAL_ORDER if signal in signals),
        primary_mechanism=mechanism,
        left_to_right_dominance_blockers=_dominance_blockers(left, right),
        right_to_left_dominance_blockers=_dominance_blockers(right, left),
    )


def audit_case(case: Case, diagnosis: Diagnosis | None = None) -> CausalSemanticsAudit:
    """Inspect one already-built deterministic Case without changing it."""
    diagnosis = diagnosis or diagnose_case(case)
    trace = diagnosis.resolution_trace
    plausible_ids = set(trace.plausible_hypotheses if trace else ())
    leading_ids = set(trace.leading_hypothesis_ids if trace else ())
    audit_by_id = {item.hypothesis_id: item for item in (trace.hypothesis_audits if trace else ())}
    snapshots_list: list[HypothesisSnapshot] = []
    for hypothesis in sorted(case.hypotheses, key=lambda item: item.hypothesis_id):
        audit = audit_by_id.get(hypothesis.hypothesis_id)
        snapshots_list.append(
            _hypothesis_snapshot(
                hypothesis,
                plausible=hypothesis.hypothesis_id in plausible_ids,
                reasons=tuple(reason.value for reason in audit.plausibility_reasons)
                if audit
                else (),
                verification=audit.verification if audit else None,
                leading=hypothesis.hypothesis_id in leading_ids,
            )
        )
    snapshots = tuple(snapshots_list)
    by_id = {item.hypothesis_id: item for item in case.hypotheses}
    competitions = tuple(
        _competition(
            by_id[left_id],
            by_id[right_id],
            {key: item.verification for key, item in audit_by_id.items()},
        )
        for index, left_id in enumerate(sorted(plausible_ids))
        for right_id in sorted(plausible_ids)[index + 1 :]
    )
    competitions = tuple(
        replace(
            item,
            both_leading=item.left_hypothesis_id in leading_ids
            and item.right_hypothesis_id in leading_ids,
        )
        for item in competitions
    )
    relevant = tuple(item for item in competitions if item.both_leading)
    mechanisms = tuple(
        sorted({item.primary_mechanism for item in relevant}, key=_MECHANISM_ORDER.index)
    )
    primary = min(mechanisms, key=_MECHANISM_ORDER.index) if mechanisms else None
    elimination_records: tuple[dict[str, object], ...] = tuple(
        cast(
            dict[str, object],
            {
                "hypothesis_id": item.hypothesis_id,
                "causal_actor": by_id[item.hypothesis_id].causal_actor.canonical
                if item.hypothesis_id in by_id
                else None,
                "code": item.code.value,
                "evidence_ids": item.evidence_ids,
                "detail": item.detail,
            },
        )
        for item in (trace.eliminations if trace else ())
    )
    signature = hypothesis_signature
    provenance_classes: set[str] = set()
    for item in snapshots:
        values = item.signature.get("provenance_classes")
        if isinstance(values, list):
            provenance_classes.update(str(value) for value in values)
    inventory = {
        "exact_temporal": any(
            item.earliest_initiating_delta_seconds is not None for item in snapshots
        ),
        "verification": any(item.verification_predicates for item in snapshots),
        "semantic_finding_differences": len(
            {key for item in snapshots for key in item.finding_keys}
        ),
        "provenance": tuple(sorted(provenance_classes)),
        "signature_fields": tuple(sorted(signature(case.hypotheses[0]).model_dump(mode="json")))
        if case.hypotheses
        else (),
        "plausibility_inputs": (
            "causal_explanation",
            "aligned_initiating_evidence",
            "contradictory_findings",
        ),
        "dominance_inputs": (
            "evidence_shape",
            "aligned_initiating_evidence_shape",
            "contradictory_findings",
        ),
        "resolution_inputs": (
            "plausible_hypotheses",
            "dominance_relations",
            "explicit_temporal_contradictions",
        ),
        "information_available_in_hypothesis": (
            "exact_actor_identity",
            "member_identity",
            "manifestation_identity",
            "exact_onset_delta",
            "exact_finding_details",
            "specific_evidence_provenance",
            "verification_predicates",
            "exact_path_entities",
        ),
        "information_represented_in_signature": (
            "initiating_kinds",
            "supporting_kinds",
            "contradiction_kinds",
            "temporal_profile",
            "symptom_relation",
            "causal_path_shape",
            "manifestation_shape",
            "provenance_classes",
        ),
        "dominance_relations": len(trace.dominance_relations) if trace else 0,
        "discriminators": len(trace.discriminators) if trace else 0,
    }
    return CausalSemanticsAudit(
        incident_id=case.incident_id,
        resolution=diagnosis.resolution.value,
        finding_count=len(case.findings),
        finding_entities=tuple(
            sorted(
                {
                    entity.canonical
                    for finding in case.findings
                    for entity in (finding.entity, *finding.related)
                }
            )
        ),
        candidate_count=len(case.candidates),
        hypothesis_count=len(case.hypotheses),
        plausible_hypothesis_ids=tuple(sorted(plausible_ids)),
        leading_hypothesis_ids=tuple(sorted(leading_ids)),
        eliminated_hypothesis_ids=tuple(sorted(trace.eliminated_hypotheses if trace else ())),
        hypothesis_snapshots=snapshots,
        candidate_snapshots=tuple(
            _candidate_snapshot(item)
            for item in sorted(case.candidates, key=lambda item: item.entity.canonical)
        ),
        competitions=competitions,
        ambiguity_mechanisms=mechanisms,
        primary_ambiguity_mechanism=primary,
        resolution_decision_basis=trace.decision_basis if trace else "",
        unresolved_dimensions=tuple(trace.unresolved_dimensions if trace else ()),
        elimination_records=elimination_records,
        stage_funnel={
            "findings": len(case.findings),
            "candidates": len(case.candidates),
            "hypotheses": len(case.hypotheses),
            "plausible": len(plausible_ids),
            "leading": len(leading_ids),
            "eliminated": len(trace.eliminated_hypotheses if trace else ()),
            "dominance_relations": len(trace.dominance_relations if trace else ()),
            "discriminators": len(trace.discriminators if trace else ()),
        },
        information_inventory=inventory,
    )


def audit_source(source: ObservationSource) -> CausalSemanticsAudit:
    case = build_case(source)
    return audit_case(case, diagnose_case(case))


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def repository_sha() -> str:
    return subprocess.check_output(("git", "rev-parse", "HEAD"), text=True).strip()


__all__ = [
    "CandidateSnapshot",
    "CausalSemanticsAudit",
    "HypothesisCompetition",
    "HypothesisSnapshot",
    "VerificationPredicateSnapshot",
    "audit_case",
    "audit_source",
    "finding_key",
    "repository_sha",
    "sha256_file",
]
