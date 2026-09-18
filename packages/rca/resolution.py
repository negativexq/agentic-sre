"""Deterministic, auditable resolution of competing causal hypotheses.

Ranking supplies an order and verification evaluates one hypothesis.  This
module answers the separate question of whether observable evidence actually
distinguishes that hypothesis from its plausible alternatives.  Scores and
canonical ordering are deliberately absent from that decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from packages.rca.model import (
    Diagnosis,
    DominanceRelation,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    HypothesisEpistemicState,
    HypothesisResolutionAudit,
    HypothesisSignature,
    Resolution,
    ResolutionDiscriminator,
    ResolutionElimination,
    ResolutionReasonCode,
    ResolutionTrace,
    VerificationTrace,
)
from packages.rca.ranking import RankingConfig
from packages.rca.temporal import (
    TemporalContradictionCertainty,
    temporal_contradiction_certainty,
)

_CHANGE_KINDS = frozenset(
    {
        FindingKind.CONFIG_CHANGE,
        FindingKind.OBJECT_CREATED,
        FindingKind.OBJECT_DELETED,
        FindingKind.SPEC_CHANGE,
        FindingKind.IMAGE_CHANGE,
        FindingKind.SCALE_CHANGE,
        FindingKind.ROLLOUT_RESTART,
        FindingKind.FAULT_INJECTION,
        FindingKind.FAULT_SCHEDULE,
        FindingKind.POLICY_CREATED,
        FindingKind.QUOTA_EXCEEDED,
        FindingKind.QUOTA_EXHAUSTED,
        FindingKind.NETWORK_RESTRICTION,
        FindingKind.AUTOSCALING_FAILURE,
        FindingKind.TRAFFIC_INCREASE,
    }
)
_MAX_TRACE_ITEMS = 8


def _temporal_label(finding: Finding) -> str:
    """Return a qualitative temporal fact without adding a new time threshold."""
    delta = finding.onset_delta_seconds
    if delta is None:
        return "missing_timestamp"
    if finding.temporal_role is EvidenceTemporalRole.INITIATING:
        return "initiating_before_onset" if delta < 0 else "initiating_near_onset"
    if finding.temporal_role is EvidenceTemporalRole.SUPPORTING:
        return "supporting_after_onset" if delta >= 0 else "supporting_before_onset"
    if finding.temporal_role is EvidenceTemporalRole.CONSEQUENCE:
        return "late_consequence" if delta >= 0 else "pre_onset_consequence"
    return "ambiguous_timing"


def _provenance_class(finding: Finding) -> str:
    """Normalize source provenance without comparing raw evidence identifiers."""
    explicit = finding.details.get("source_class") or finding.details.get("source")
    if isinstance(explicit, str) and explicit:
        return explicit.casefold()
    if finding.kind is FindingKind.FAILURE_EVENT:
        return "kubernetes_event"
    if finding.kind is FindingKind.DEPENDENCY_ERRORS:
        return "log"
    if finding.kind in {FindingKind.TRAFFIC_INCREASE, FindingKind.RESOURCE_PRESSURE}:
        return "metric"
    if finding.kind in _CHANGE_KINDS:
        return "object_observation"
    return "derived_observation"


def _path_shape(hypothesis: Hypothesis) -> tuple[tuple[tuple[str, str, str], ...], ...]:
    paths = {
        tuple((hop.source.kind, hop.relation, hop.target.kind) for hop in path)
        for path in hypothesis.causal_paths
        if path
    }
    return tuple(sorted(paths))


def hypothesis_signature(hypothesis: Hypothesis) -> HypothesisSignature:
    """Build a deterministic, entity-name-independent evidence signature."""
    initiating = tuple(sorted({finding.kind.value for finding in hypothesis.initiating_findings}))
    supporting = tuple(sorted({finding.kind.value for finding in hypothesis.supporting_findings}))
    contradictions = tuple(
        sorted({finding.kind.value for finding in hypothesis.contradictory_findings})
    )
    temporal = tuple(sorted({_temporal_label(finding) for finding in hypothesis.findings}))
    manifestation_shape = tuple(
        sorted(
            {
                f"{member.kind}:{finding.kind.value}"
                for member in hypothesis.manifestations
                for finding in hypothesis.findings
                if finding.entity == member
            }
        )
    )
    provenance = tuple(sorted({_provenance_class(finding) for finding in hypothesis.findings}))
    return HypothesisSignature(
        initiating_kinds=initiating,
        supporting_kinds=supporting,
        contradiction_kinds=contradictions,
        temporal_profile=temporal,
        symptom_relation=hypothesis.causal_explanation,
        causal_path_shape=_path_shape(hypothesis),
        manifestation_shape=manifestation_shape,
        provenance_classes=provenance,
    )


def signatures_for(hypotheses: Iterable[Hypothesis]) -> tuple[HypothesisSignature, ...]:
    """Return signatures in the supplied order for compatibility."""
    return tuple(hypothesis_signature(hypothesis) for hypothesis in hypotheses)


def structurally_equivalent(left: Hypothesis, right: Hypothesis) -> bool:
    """Whether two hypotheses carry materially equivalent causal support."""
    return hypothesis_signature(left) == hypothesis_signature(right)


def _has_aligned_initiating(hypothesis: Hypothesis) -> bool:
    return any(
        finding.temporal_role is EvidenceTemporalRole.INITIATING and finding.kind in _CHANGE_KINDS
        for finding in hypothesis.findings
    )


@dataclass(frozen=True)
class HypothesisAssessment:
    """One centralized epistemic assessment used by resolution and its audit."""

    state: HypothesisEpistemicState
    reason_codes: tuple[ResolutionReasonCode, ...]
    hard_contradiction_findings: tuple[Finding, ...]


def assess_hypothesis(
    hypothesis: Hypothesis,
    *,
    onset_grace: timedelta | None = None,
) -> HypothesisAssessment:
    """Separate hard contradiction from missing positive causal proof."""
    grace = onset_grace or RankingConfig().verification_onset_grace
    reasons: list[ResolutionReasonCode] = []
    if hypothesis.causal_explanation not in {"PATH", "DIRECT"}:
        reasons.append(ResolutionReasonCode.NO_CAUSAL_SYMPTOM_LINK)
    if not _has_aligned_initiating(hypothesis):
        reasons.append(ResolutionReasonCode.NO_ONSET_CAPABLE_INITIATING_EVIDENCE)

    hard_findings: list[Finding] = []
    temporal_findings = tuple(
        temporal_contradiction_certainty(finding, grace)
        for finding in hypothesis.contradictory_findings
    )
    if hypothesis.contradictory_findings:
        reasons.append(ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION)
        hard_findings.extend(
            finding
            for finding, certainty in zip(
                hypothesis.contradictory_findings,
                temporal_findings,
                strict=True,
            )
            if certainty is TemporalContradictionCertainty.DEFINITELY_LATE
        )

    if ResolutionReasonCode.NO_CAUSAL_SYMPTOM_LINK in reasons:
        state = HypothesisEpistemicState.CONTRADICTED
    elif hard_findings:
        state = HypothesisEpistemicState.CONTRADICTED
    elif hypothesis.contradictory_findings:
        state = HypothesisEpistemicState.UNRESOLVED
    elif _has_aligned_initiating(hypothesis):
        state = HypothesisEpistemicState.SUPPORTED
    else:
        state = HypothesisEpistemicState.UNRESOLVED
    return HypothesisAssessment(
        state=state,
        reason_codes=tuple(dict.fromkeys(reasons)),
        hard_contradiction_findings=tuple(hard_findings),
    )


def _evidence_key(finding: Finding) -> tuple[str, str, str]:
    return finding.kind.value, finding.temporal_role.value, _provenance_class(finding)


def _evidence_ids(hypothesis: Hypothesis) -> tuple[str, ...]:
    return tuple(
        sorted(
            {evidence_id for finding in hypothesis.findings for evidence_id in finding.evidence_ids}
        )
    )


def _plausibility_reasons(
    hypothesis: Hypothesis,
    *,
    onset_grace: timedelta | None = None,
) -> tuple[ResolutionReasonCode, ...]:
    return assess_hypothesis(hypothesis, onset_grace=onset_grace).reason_codes


def dominates(
    stronger: Hypothesis,
    weaker: Hypothesis,
    *,
    onset_grace: timedelta | None = None,
) -> bool:
    """Return true only for evidence inclusion with a causal discriminator.

    Scores, candidate rank, and canonical entity order are never used to
    eliminate a hypothesis.  A stronger hypothesis must contain the weaker
    evidence shape and add onset-capable causal support.
    """
    if (
        assess_hypothesis(stronger, onset_grace=onset_grace).state
        is not HypothesisEpistemicState.SUPPORTED
        or assess_hypothesis(weaker, onset_grace=onset_grace).state
        is not HypothesisEpistemicState.SUPPORTED
    ):
        return False
    stronger_keys = {_evidence_key(finding) for finding in stronger.findings}
    weaker_keys = {_evidence_key(finding) for finding in weaker.findings}
    if not weaker_keys <= stronger_keys:
        return False
    stronger_initiating = {
        _evidence_key(finding)
        for finding in stronger.initiating_findings
        if finding.temporal_role is EvidenceTemporalRole.INITIATING
    }
    weaker_initiating = {
        _evidence_key(finding)
        for finding in weaker.initiating_findings
        if finding.temporal_role is EvidenceTemporalRole.INITIATING
    }
    # Extra support/consequence shapes are not causal discrimination.  The
    # stronger episode must add an aligned initiating evidence shape; otherwise
    # a late failure or a larger manifestation set would manufacture
    # dominance.
    return stronger_initiating > weaker_initiating


def _elimination_for(
    hypothesis: Hypothesis,
    assessment: HypothesisAssessment,
) -> ResolutionElimination:
    reasons = assessment.reason_codes
    code = (
        ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION
        if assessment.hard_contradiction_findings
        else ResolutionReasonCode.NO_CAUSAL_SYMPTOM_LINK
        if ResolutionReasonCode.NO_CAUSAL_SYMPTOM_LINK in reasons
        else ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION
    )
    details = {
        ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION: "contains explicit temporal contradiction",
        ResolutionReasonCode.NO_CAUSAL_SYMPTOM_LINK: "does not causally reach an alerting symptom",
        ResolutionReasonCode.NO_ONSET_CAPABLE_INITIATING_EVIDENCE: "has no onset-capable initiating evidence",
    }
    return ResolutionElimination(
        hypothesis_id=hypothesis.hypothesis_id,
        code=code,
        evidence_ids=_evidence_ids(hypothesis),
        detail=details[code],
    )


def _legacy_elimination_reason(item: ResolutionElimination) -> str:
    if item.code is ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION:
        return f"{item.hypothesis_id}: contradictory evidence"
    return (
        f"{item.hypothesis_id}: lacks onset-capable initiating evidence or causal symptom linkage"
    )


def _dominance_relation(stronger: Hypothesis, weaker: Hypothesis) -> DominanceRelation:
    stronger_ids = set(_evidence_ids(stronger))
    weaker_ids = set(_evidence_ids(weaker))
    ids = tuple(sorted(stronger_ids - weaker_ids or stronger_ids))
    return DominanceRelation(
        stronger_hypothesis_id=stronger.hypothesis_id,
        weaker_hypothesis_id=weaker.hypothesis_id,
        evidence_ids=ids[:12],
        detail=(
            "additional onset-capable evidence explains the weaker hypothesis's "
            "manifestation; score and identity ordering were not used"
        ),
    )


def _discriminator(
    kind: str,
    stronger: Hypothesis,
    weaker: Hypothesis | None = None,
    detail: str = "",
) -> ResolutionDiscriminator:
    hypotheses = (stronger,) if weaker is None else (stronger, weaker)
    signatures = tuple(hypothesis_signature(item) for item in hypotheses)
    return ResolutionDiscriminator(
        kind=kind,
        hypothesis_ids=tuple(item.hypothesis_id for item in hypotheses),
        evidence_ids=tuple(sorted({item for h in hypotheses for item in _evidence_ids(h)}))[:12],
        finding_kinds=tuple(
            sorted({finding.kind.value for h in hypotheses for finding in h.findings})
        ),
        temporal_relations=tuple(
            sorted({label for sig in signatures for label in sig.temporal_profile})
        ),
        causal_path_shapes=tuple(path for sig in signatures for path in sig.causal_path_shape)[:8],
        detail=detail,
    )


def _audit_items(
    hypotheses: Sequence[Hypothesis],
    verification_traces: Mapping[str, VerificationTrace] | None,
    focus_ids: Sequence[str] = (),
    onset_grace: timedelta | None = None,
) -> tuple[HypothesisResolutionAudit, ...]:
    audits: list[HypothesisResolutionAudit] = []
    focused = tuple(
        hypothesis for hypothesis in hypotheses if hypothesis.hypothesis_id in set(focus_ids)
    )
    remaining = tuple(
        hypothesis
        for hypothesis in sorted(hypotheses, key=lambda item: item.hypothesis_id)
        if hypothesis.hypothesis_id not in {item.hypothesis_id for item in focused}
    )
    for hypothesis in (*focused, *remaining)[:_MAX_TRACE_ITEMS]:
        assessment = assess_hypothesis(hypothesis, onset_grace=onset_grace)
        reasons = assessment.reason_codes
        signature = hypothesis_signature(hypothesis)
        audits.append(
            HypothesisResolutionAudit(
                hypothesis_id=hypothesis.hypothesis_id,
                signature=signature,
                epistemic_state=assessment.state,
                plausible=assessment.state is HypothesisEpistemicState.SUPPORTED,
                plausibility_reasons=reasons,
                verification=(verification_traces or {}).get(hypothesis.hypothesis_id),
                contradictory_evidence_ids=tuple(
                    sorted(
                        evidence_id
                        for finding in hypothesis.contradictory_findings
                        for evidence_id in finding.evidence_ids
                    )
                )[:12],
                onset_relation=signature.temporal_profile,
                causal_linkage=hypothesis.causal_explanation,
            )
        )
    return tuple(audits)


def _trace(base: Mapping[str, object], **extra: object) -> ResolutionTrace:
    """Validate a dynamically assembled trace without weakening its schema."""
    return ResolutionTrace.model_validate({**base, **extra})


def _attach_audits(
    trace: ResolutionTrace,
    hypotheses: Sequence[Hypothesis],
    verification_traces: Mapping[str, VerificationTrace] | None,
    onset_grace: timedelta | None = None,
) -> ResolutionTrace:
    focus_ids = tuple(
        dict.fromkeys(
            (
                *trace.leading_hypothesis_ids,
                *(item.hypothesis_id for item in hypotheses[:_MAX_TRACE_ITEMS]),
            )
        )
    )
    return trace.model_copy(
        update={
            "hypothesis_audits": _audit_items(
                hypotheses,
                verification_traces,
                focus_ids,
                onset_grace,
            ),
        }
    )


def resolve_hypotheses(
    hypotheses: Sequence[Hypothesis],
    *,
    verification_traces: Mapping[str, VerificationTrace] | None = None,
    onset_grace: timedelta | None = None,
) -> ResolutionTrace:
    """Resolve distinguishability without treating missing proof as contradiction."""
    considered = tuple(sorted(h.hypothesis_id for h in hypotheses))
    audits = _audit_items(hypotheses, verification_traces, onset_grace=onset_grace)
    if not hypotheses:
        return ResolutionTrace(
            state=Resolution.INSUFFICIENT_EVIDENCE,
            considered_hypotheses=(),
            hypothesis_audits=(),
            decision_basis="NO_PLAUSIBLE_HYPOTHESIS",
            rationale="No causal hypothesis was generated from the available observations.",
        )

    by_id = {hypothesis.hypothesis_id: hypothesis for hypothesis in hypotheses}
    assessments = {
        hypothesis.hypothesis_id: assess_hypothesis(hypothesis, onset_grace=onset_grace)
        for hypothesis in hypotheses
    }
    supported = tuple(
        sorted(
            (
                hypothesis
                for hypothesis in hypotheses
                if assessments[hypothesis.hypothesis_id].state is HypothesisEpistemicState.SUPPORTED
            ),
            key=lambda item: item.hypothesis_id,
        )
    )
    unresolved = tuple(
        sorted(
            (
                hypothesis
                for hypothesis in hypotheses
                if assessments[hypothesis.hypothesis_id].state
                is HypothesisEpistemicState.UNRESOLVED
            ),
            key=lambda item: item.hypothesis_id,
        )
    )
    contradicted = tuple(
        sorted(
            (
                hypothesis
                for hypothesis in hypotheses
                if assessments[hypothesis.hypothesis_id].state
                is HypothesisEpistemicState.CONTRADICTED
            ),
            key=lambda item: item.hypothesis_id,
        )
    )
    eliminations = tuple(
        _elimination_for(hypothesis, assessments[hypothesis.hypothesis_id])
        for hypothesis in contradicted[:_MAX_TRACE_ITEMS]
    )
    legacy_reasons = tuple(_legacy_elimination_reason(item) for item in eliminations)
    signatures = tuple(hypothesis_signature(hypothesis) for hypothesis in supported)
    base = dict(
        considered_hypotheses=considered,
        plausible_hypotheses=tuple(h.hypothesis_id for h in supported),
        unresolved_hypotheses=tuple(h.hypothesis_id for h in unresolved),
        eliminated_hypotheses=tuple(h.hypothesis_id for h in contradicted),
        elimination_reasons=legacy_reasons,
        eliminations=eliminations,
        signatures=signatures[:_MAX_TRACE_ITEMS],
        hypothesis_audits=audits,
    )
    if not supported and unresolved:
        return _attach_audits(
            _trace(
                base,
                state=Resolution.INSUFFICIENT_EVIDENCE,
                decision_basis="NO_SUPPORTED_HYPOTHESIS_WITH_UNRESOLVED_ALTERNATIVES",
                rationale=(
                    "Causally possible hypotheses remain, but current evidence does not "
                    "positively establish one."
                ),
            ),
            hypotheses,
            verification_traces,
            onset_grace,
        )
    if not supported:
        return _attach_audits(
            _trace(
                base,
                state=Resolution.INSUFFICIENT_EVIDENCE,
                decision_basis="NO_PLAUSIBLE_HYPOTHESIS",
                rationale="All generated hypotheses contain a hard causal contradiction.",
            ),
            hypotheses,
            verification_traces,
            onset_grace,
        )

    dominance_relations = tuple(
        _dominance_relation(stronger, weaker)
        for stronger in supported
        for weaker in supported
        if stronger.hypothesis_id != weaker.hypothesis_id
        and dominates(stronger, weaker, onset_grace=onset_grace)
    )
    dominators = tuple(
        hypothesis
        for hypothesis in supported
        if all(
            hypothesis.hypothesis_id == other.hypothesis_id
            or dominates(hypothesis, other, onset_grace=onset_grace)
            for other in supported
        )
    )
    contradictions = tuple(
        item
        for item in eliminations
        if item.code is ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION
    )

    if unresolved:
        leading_ids = tuple(
            sorted(
                (
                    *[item.hypothesis_id for item in supported],
                    *[item.hypothesis_id for item in unresolved],
                )
            )
        )
        return _attach_audits(
            _trace(
                base,
                state=Resolution.AMBIGUOUS,
                leading_hypothesis_ids=leading_ids,
                unresolved_dimensions=(
                    "causal_actor_identity",
                    "initiating_evidence_source",
                    "symptom_attribution",
                ),
                dominance_relations=dominance_relations[:_MAX_TRACE_ITEMS],
                decision_basis="UNRESOLVED_CAUSAL_ALTERNATIVE",
                rationale=(
                    "At least one causally possible hypothesis remains unresolved; "
                    "missing proof does not exclude it from deterministic resolution."
                ),
            ),
            hypotheses,
            verification_traces,
            onset_grace,
        )

    if len(supported) == 1:
        selected = supported[0]
        if contradictions:
            discriminator = _discriminator(
                "VALID_CONTRADICTION",
                selected,
                detail="all competing hypotheses were excluded by explicit temporal contradiction",
            )
            basis = "VALID_CONTRADICTION"
        else:
            discriminator = _discriminator(
                "ONLY_ONE_PLAUSIBLE",
                selected,
                detail="all alternatives failed a causal linkage or onset-capable evidence predicate",
            )
            basis = "ONLY_ONE_PLAUSIBLE"
        return _attach_audits(
            _trace(
                base,
                state=Resolution.RESOLVED,
                leading_hypothesis_ids=(selected.hypothesis_id,),
                distinguishing_facts=(discriminator.detail,),
                discriminators=(discriminator,),
                dominance_relations=dominance_relations[:_MAX_TRACE_ITEMS],
                decision_basis=basis,
                rationale="The leading hypothesis is deterministically distinguished by its causal evidence.",
            ),
            hypotheses,
            verification_traces,
            onset_grace,
        )

    if len(dominators) == 1:
        selected = dominators[0]
        relations = tuple(
            relation
            for relation in dominance_relations
            if relation.stronger_hypothesis_id == selected.hypothesis_id
        )
        discriminators = tuple(
            _discriminator(
                "VALID_DOMINANCE",
                selected,
                by_id[relation.weaker_hypothesis_id],
                detail=relation.detail,
            )
            for relation in relations[:_MAX_TRACE_ITEMS]
        )
        return _attach_audits(
            _trace(
                base,
                state=Resolution.RESOLVED,
                leading_hypothesis_ids=(selected.hypothesis_id,),
                distinguishing_facts=tuple(item.detail for item in discriminators),
                discriminators=discriminators,
                dominance_relations=dominance_relations[:_MAX_TRACE_ITEMS],
                decision_basis="VALID_DOMINANCE",
                rationale="The selected hypothesis contains additional onset-capable evidence that explains the competing manifestation.",
            ),
            hypotheses,
            verification_traces,
            onset_grace,
        )

    leading = tuple(
        hypothesis
        for hypothesis in supported
        if not any(
            dominates(other, hypothesis, onset_grace=onset_grace)
            for other in supported
            if other.hypothesis_id != hypothesis.hypothesis_id
        )
    )
    return _attach_audits(
        _trace(
            base,
            state=Resolution.AMBIGUOUS,
            leading_hypothesis_ids=tuple(h.hypothesis_id for h in leading[:_MAX_TRACE_ITEMS]),
            unresolved_dimensions=(
                "causal_actor_identity",
                "initiating_evidence_source",
                "symptom_attribution",
            ),
            dominance_relations=dominance_relations[:_MAX_TRACE_ITEMS],
            decision_basis="EQUIVALENT_OR_INCOMPARABLE_PLAUSIBLE_HYPOTHESES",
            rationale=(
                "Multiple plausible hypotheses remain and no evidence-backed dominance or "
                "contradiction distinguishes one causal actor from the others."
            ),
        ),
        hypotheses,
        verification_traces,
        onset_grace,
    )


def resolution_audit_records(diagnosis: Diagnosis) -> list[dict[str, object]]:
    """Return bounded near-collision records for a stored diagnosis."""
    trace = diagnosis.resolution_trace
    if trace is None or len(trace.hypothesis_audits) < 2:
        return []
    audits = trace.hypothesis_audits
    final_selected_id = trace.leading_hypothesis_ids[0] if trace.leading_hypothesis_ids else None
    final_selected = next(
        (audit for audit in audits if audit.hypothesis_id == final_selected_id), None
    )
    plausible_ids = {audit.hypothesis_id for audit in audits if audit.plausible}
    records: list[dict[str, object]] = []
    for index, left in enumerate(audits):
        for right in audits[index + 1 :]:
            if left.signature != right.signature:
                continue
            pair_ids = {left.hypothesis_id, right.hypothesis_id}
            pair_has_plausible = bool(pair_ids & plausible_ids)
            resolved_by_single_plausible = (
                diagnosis.resolution is Resolution.RESOLVED
                and final_selected is not None
                and not pair_has_plausible
            )
            relevant_to_resolution = pair_has_plausible or bool(
                final_selected is not None
                and (final_selected.hypothesis_id in pair_ids or resolved_by_single_plausible)
            )
            if not relevant_to_resolution:
                continue
            relation = next(
                (
                    item
                    for item in trace.dominance_relations
                    if {
                        item.stronger_hypothesis_id,
                        item.weaker_hypothesis_id,
                    }
                    == {left.hypothesis_id, right.hypothesis_id}
                ),
                None,
            )
            if relation is not None:
                classification = "VALID_DOMINANCE"
            elif (
                left.epistemic_state is not HypothesisEpistemicState.SUPPORTED
                or right.epistemic_state is not HypothesisEpistemicState.SUPPORTED
            ):
                if (
                    left.epistemic_state is HypothesisEpistemicState.UNRESOLVED
                    or right.epistemic_state is HypothesisEpistemicState.UNRESOLVED
                ):
                    classification = "UNRESOLVED_PEER"
                else:
                    classification = (
                        "VALID_CONTRADICTION"
                        if ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION
                        in (*left.plausibility_reasons, *right.plausibility_reasons)
                        else "ONLY_ONE_PLAUSIBLE"
                    )
            elif diagnosis.resolution is Resolution.AMBIGUOUS:
                classification = "AMBIGUOUS_PEER"
            else:
                classification = "SCORE_LEAKAGE"
            if final_selected is not None and (
                final_selected.hypothesis_id in pair_ids or resolved_by_single_plausible
            ):
                selected, alternative = (
                    (final_selected, right)
                    if final_selected.hypothesis_id == left.hypothesis_id
                    else (final_selected, left)
                )
            else:
                selected, alternative = left, right
            discriminators = [
                item.model_dump(mode="json")
                for item in trace.discriminators
                if set(item.hypothesis_ids) & {left.hypothesis_id, right.hypothesis_id}
            ]
            records.append(
                {
                    "incident_id": diagnosis.incident_id,
                    "selected": selected.model_dump(mode="json"),
                    "alternative": alternative.model_dump(mode="json"),
                    "resolution_leader": (
                        final_selected.model_dump(mode="json") if final_selected else None
                    ),
                    "comparison_pair": [
                        left.model_dump(mode="json"),
                        right.model_dump(mode="json"),
                    ],
                    "dominance": relation.model_dump(mode="json") if relation else None,
                    "discriminators": discriminators,
                    "classification": classification,
                    "resolution": diagnosis.resolution.value,
                    "resolution_reason": trace.rationale,
                }
            )
    return records[:_MAX_TRACE_ITEMS]


def summarize_resolution_audit(diagnoses: Iterable[Diagnosis]) -> dict[str, object]:
    items = tuple(diagnoses)
    records = [record for diagnosis in items for record in resolution_audit_records(diagnosis)]
    counts: dict[str, int] = {}
    for record in records:
        classification = str(record["classification"])
        counts[classification] = counts.get(classification, 0) + 1
    return {
        "diagnoses_audited": len(items),
        "structural_near_collisions": len(records),
        "classifications": counts,
        "records": records,
    }


def summarize_resolutions(diagnoses: Iterable[Diagnosis]) -> dict[str, int | float]:
    """Summarize resolution diagnostics without consulting benchmark labels."""
    items = tuple(diagnoses)
    ambiguous = [item for item in items if item.resolution is Resolution.AMBIGUOUS]
    sizes = [len(item.ambiguous_hypotheses) for item in ambiguous]
    collisions = sum(
        1
        for item in ambiguous
        if item.resolution_trace is not None and len(item.resolution_trace.signatures) > 1
    )
    return {
        "resolved": sum(item.resolution is Resolution.RESOLVED for item in items),
        "ambiguous": len(ambiguous),
        "insufficient_evidence": sum(
            item.resolution is Resolution.INSUFFICIENT_EVIDENCE for item in items
        ),
        "average_ambiguity_set_size": round(sum(sizes) / len(sizes), 3) if sizes else 0.0,
        "structural_top_hypothesis_collisions": collisions,
    }


__all__ = [
    "HypothesisAssessment",
    "assess_hypothesis",
    "dominates",
    "hypothesis_signature",
    "resolve_hypotheses",
    "resolution_audit_records",
    "signatures_for",
    "summarize_resolution_audit",
    "summarize_resolutions",
    "structurally_equivalent",
]
