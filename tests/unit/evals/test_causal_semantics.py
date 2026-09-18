"""Ground-truth-blind tests for the P2A causal semantics lab."""

from __future__ import annotations

from datetime import UTC, datetime

from packages.evals.causal_semantics import _competition, audit_source
from packages.rca.demo import demo_source
from packages.rca.model import (
    Confidence,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    PredicateStatus,
    VerificationPredicate,
    VerificationTrace,
)


def _finding(
    entity: EntityRef,
    evidence: str,
    *,
    delta: float = -30.0,
    summary: str = "changed",
) -> Finding:
    return Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=entity,
        at=datetime(2026, 1, 1, tzinfo=UTC),
        summary=summary,
        evidence_ids=(evidence,),
        temporal_role=EvidenceTemporalRole.INITIATING,
        incident_onset=datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC),
        onset_delta_seconds=delta,
    )


def _hypothesis(name: str, finding: Finding, *, score: float = 1.0) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=f"h:{name}",
        causal_actor=finding.entity,
        findings=(finding,),
        initiating_findings=(finding,),
        causal_explanation="DIRECT",
        score=score,
    )


def test_exact_signature_collision_is_detected_without_score() -> None:
    left = _hypothesis("left", _finding(EntityRef.parse("ns/Deployment/left"), "raw:left"))
    right = _hypothesis("right", _finding(EntityRef.parse("ns/Deployment/right"), "raw:right"))
    pair = _competition(left, right, {})
    assert pair.exact_signature_equal
    assert "EXACT_SIGNATURE_COLLISION" in pair.diagnostic_signals
    assert pair.primary_mechanism == "SIGNATURE_COLLISION"
    changed = _competition(left.model_copy(update={"score": 999.0}), right, {})
    assert changed.diagnostic_signals == pair.diagnostic_signals
    assert changed.primary_mechanism == pair.primary_mechanism


def test_disjoint_causes_and_pair_order_are_symmetric() -> None:
    left = _hypothesis("left", _finding(EntityRef.parse("ns/Deployment/left"), "raw:left"))
    right = _hypothesis(
        "right",
        _finding(EntityRef.parse("other/Service/right"), "raw:right").model_copy(
            update={"kind": FindingKind.FAILURE_EVENT}
        ),
    )
    forward = _competition(left, right, {})
    reverse = _competition(right, left, {})
    assert "DISJOINT_PLAUSIBLE_CAUSES" in forward.diagnostic_signals
    assert forward.primary_mechanism == "UNUSED_DIAGNOSTIC_SIGNAL_PRESENT"
    assert reverse.primary_mechanism == forward.primary_mechanism
    assert reverse.diagnostic_signals == forward.diagnostic_signals


def test_temporal_and_verification_differences_are_observable() -> None:
    left_finding = _finding(EntityRef.parse("ns/Deployment/left"), "raw:left", delta=-90.0)
    right_finding = _finding(EntityRef.parse("ns/Deployment/right"), "raw:right", delta=-5.0)
    left = _hypothesis("left", left_finding)
    right = _hypothesis("right", right_finding)
    left_trace = VerificationTrace(
        candidate=left.causal_actor,
        decision=Confidence.VERIFIED,
        predicates=(
            VerificationPredicate(
                name="test_predicate",
                status=PredicateStatus.PASS,
            ),
        ),
    )
    right_trace = left_trace.model_copy(
        update={
            "candidate": right.causal_actor,
            "decision": Confidence.UNVERIFIED,
            "predicates": (
                VerificationPredicate(
                    name="test_predicate",
                    status=PredicateStatus.FAIL,
                ),
            ),
        }
    )
    pair = _competition(
        left,
        right,
        {left.hypothesis_id: left_trace, right.hypothesis_id: right_trace},
    )
    assert "EXACT_TEMPORAL_ORDER_AVAILABLE" in pair.diagnostic_signals
    assert pair.left_earliest_initiating_delta == -90.0
    assert pair.right_earliest_initiating_delta == -5.0
    assert "VERIFICATION_DECISION_DIFFERENCE" in pair.diagnostic_signals
    assert "VERIFICATION_PREDICATE_DIFFERENCE" in pair.diagnostic_signals


def test_semantic_evidence_superset_without_initiating_discriminator_is_reported() -> None:
    base = _finding(EntityRef.parse("ns/Deployment/left"), "raw:left")
    consequence = _finding(
        EntityRef.parse("ns/Deployment/left"),
        "raw:failure",
        summary="failed",
    ).model_copy(
        update={
            "kind": FindingKind.FAILURE_EVENT,
            "temporal_role": EvidenceTemporalRole.SUPPORTING,
        }
    )
    stronger = _hypothesis("stronger", base).model_copy(
        update={"findings": (base, consequence), "supporting_findings": (consequence,)}
    )
    weaker = _hypothesis("weaker", base)
    pair = _competition(stronger, weaker, {})
    assert not pair.left_dominates_right
    assert "SEMANTIC_EVIDENCE_SUPERSET_WITHOUT_DOMINANCE" in pair.diagnostic_signals
    assert "NO_ADDITIONAL_INITIATING_EVIDENCE_SHAPE" in pair.left_to_right_dominance_blockers


def test_entity_renaming_does_not_change_structural_diagnostics() -> None:
    left = _hypothesis("left", _finding(EntityRef.parse("ns/Deployment/a"), "raw:left"))
    right = _hypothesis("right", _finding(EntityRef.parse("ns/Deployment/b"), "raw:right"))
    renamed_left = _hypothesis(
        "renamed-left", _finding(EntityRef.parse("other/Deployment/x"), "raw:other-left")
    )
    renamed_right = _hypothesis(
        "renamed-right", _finding(EntityRef.parse("other/Deployment/y"), "raw:other-right")
    )
    assert (
        _competition(left, right, {}).primary_mechanism
        == _competition(renamed_left, renamed_right, {}).primary_mechanism
    )


def test_real_inmemory_production_path_audit_is_ground_truth_blind() -> None:
    audit = audit_source(demo_source())
    assert audit.incident_id == "demo-bad-rollout"
    assert audit.finding_count > 0
    assert audit.stage_funnel["findings"] == audit.finding_count
    assert all("ground" not in key.casefold() for key in audit.information_inventory)
    assert audit.as_dict() == audit_source(demo_source()).as_dict()
