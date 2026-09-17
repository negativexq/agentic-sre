"""Deterministic resolution of competing causal hypotheses.

Ranking gives a stable order and verification checks one hypothesis at a time.
This module answers the separate question of whether the evidence distinguishes
the leading hypothesis from its plausible alternatives.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from packages.rca.model import (
    Diagnosis,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    HypothesisSignature,
    Resolution,
    ResolutionTrace,
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
    if finding.kind in {FindingKind.DEPENDENCY_ERRORS}:
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
    """Return signatures in the supplied deterministic hypothesis order."""
    return tuple(hypothesis_signature(hypothesis) for hypothesis in hypotheses)


def structurally_equivalent(left: Hypothesis, right: Hypothesis) -> bool:
    """Whether two hypotheses carry materially equivalent causal support."""
    return hypothesis_signature(left) == hypothesis_signature(right)


def _has_aligned_initiating(hypothesis: Hypothesis) -> bool:
    return any(
        finding.temporal_role is EvidenceTemporalRole.INITIATING and finding.kind in _CHANGE_KINDS
        for finding in hypothesis.findings
    )


def _evidence_key(finding: Finding) -> tuple[str, str, str]:
    return finding.kind.value, finding.temporal_role.value, _provenance_class(finding)


def dominates(stronger: Hypothesis, weaker: Hypothesis) -> bool:
    """Return true only for evidence inclusion with a causal discriminator.

    This is deliberately structural.  Scores and canonical entity order are
    never used to eliminate a hypothesis.
    """
    if stronger.contradictory_findings or not _has_aligned_initiating(stronger):
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
    return stronger_initiating > weaker_initiating or len(stronger_keys) > len(weaker_keys)


def _plausible(hypothesis: Hypothesis) -> bool:
    """A plausible peer needs causal linkage and onset-capable evidence."""
    return (
        hypothesis.causal_explanation in {"PATH", "DIRECT"}
        and _has_aligned_initiating(hypothesis)
        and not hypothesis.contradictory_findings
    )


def resolve_hypotheses(hypotheses: Sequence[Hypothesis]) -> ResolutionTrace:
    """Resolve distinguishability without a score or margin threshold."""
    if not hypotheses:
        return ResolutionTrace(
            state=Resolution.INSUFFICIENT_EVIDENCE,
            rationale="No causal hypothesis was generated from the available observations.",
        )

    signatures = signatures_for(hypotheses)
    plausible = [hypothesis for hypothesis in hypotheses if _plausible(hypothesis)]
    eliminated = [hypothesis for hypothesis in hypotheses if hypothesis not in plausible]
    elimination_reasons = [
        f"{hypothesis.hypothesis_id}: "
        + (
            "contains contradictory evidence"
            if hypothesis.contradictory_findings
            else "lacks onset-capable initiating evidence or causal symptom linkage"
        )
        for hypothesis in eliminated
    ]
    if not plausible:
        return ResolutionTrace(
            state=Resolution.INSUFFICIENT_EVIDENCE,
            signatures=signatures[:_MAX_TRACE_ITEMS],
            eliminated_hypotheses=tuple(h.hypothesis_id for h in eliminated[:_MAX_TRACE_ITEMS]),
            elimination_reasons=tuple(elimination_reasons[:_MAX_TRACE_ITEMS]),
            rationale="No hypothesis contains sufficient initiating causal evidence.",
        )

    selected = plausible[0]
    peers = [
        hypothesis
        for hypothesis in plausible
        if hypothesis.hypothesis_id != selected.hypothesis_id
        and structurally_equivalent(selected, hypothesis)
        and not any(dominates(other, hypothesis) for other in plausible if other != hypothesis)
    ]
    leading = [selected, *peers]
    if peers:
        return ResolutionTrace(
            state=Resolution.AMBIGUOUS,
            leading_hypothesis_ids=tuple(h.hypothesis_id for h in leading[:_MAX_TRACE_ITEMS]),
            signatures=tuple(hypothesis_signature(h) for h in leading[:_MAX_TRACE_ITEMS]),
            unresolved_dimensions=(
                "causal_actor_identity",
                "initiating_evidence_source",
                "symptom_attribution",
            ),
            eliminated_hypotheses=tuple(h.hypothesis_id for h in eliminated[:_MAX_TRACE_ITEMS]),
            elimination_reasons=tuple(elimination_reasons[:_MAX_TRACE_ITEMS]),
            rationale=(
                "Two or more hypotheses have equivalent initiating evidence, temporal "
                "alignment, and symptom linkage; no deterministic discriminating evidence "
                "is available."
            ),
        )

    facts: list[str] = []
    if selected.initiating_findings:
        facts.append("the selected hypothesis has onset-aligned initiating evidence")
    if eliminated:
        facts.append("alternative hypotheses lack equivalent onset-capable causal support")
    return ResolutionTrace(
        state=Resolution.RESOLVED,
        leading_hypothesis_ids=(selected.hypothesis_id,),
        signatures=(hypothesis_signature(selected),),
        distinguishing_facts=tuple(facts),
        eliminated_hypotheses=tuple(h.hypothesis_id for h in eliminated[:_MAX_TRACE_ITEMS]),
        elimination_reasons=tuple(elimination_reasons[:_MAX_TRACE_ITEMS]),
        rationale="The leading hypothesis is deterministically distinguished by its causal evidence.",
    )


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
    "dominates",
    "hypothesis_signature",
    "resolve_hypotheses",
    "signatures_for",
    "summarize_resolutions",
    "structurally_equivalent",
]
