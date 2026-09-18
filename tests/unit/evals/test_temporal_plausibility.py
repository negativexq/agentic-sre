"""Ground-truth-blind P2D temporal and epistemic regressions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from packages.evals.temporal_plausibility import (
    HypothesisTemporalAudit,
    counterfactual_resolution,
    temporal_evidence_snapshots,
)
from packages.rca.model import (
    ClusterEvent,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Lifecycle,
    ObjectVersion,
)
from packages.rca.source import InMemorySource

ONSET = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _change_source(previous: datetime, current: datetime) -> InMemorySource:
    entity = EntityRef.parse("shop/Deployment/checkout")
    return InMemorySource(
        name="temporal-unit",
        versions=[
            ObjectVersion(
                entity=entity,
                observed_at=previous,
                body={
                    "kind": "Deployment",
                    "metadata": {"name": "checkout", "namespace": "shop"},
                    "spec": {"replicas": 1},
                },
                evidence_id="raw:before",
                lifecycle=Lifecycle.OBSERVED,
            ),
            ObjectVersion(
                entity=entity,
                observed_at=current,
                body={
                    "kind": "Deployment",
                    "metadata": {"name": "checkout", "namespace": "shop"},
                    "spec": {"replicas": 2},
                },
                evidence_id="raw:after",
                lifecycle=Lifecycle.UPDATED,
            ),
        ],
    )


def _change_finding(current: datetime) -> Finding:
    return Finding(
        kind=FindingKind.SPEC_CHANGE,
        entity=EntityRef.parse("shop/Deployment/checkout"),
        at=current,
        summary="spec changed",
        evidence_ids=("raw:before", "raw:after"),
        temporal_role=EvidenceTemporalRole.CONSEQUENCE,
        incident_onset=ONSET,
        onset_delta_seconds=(current - ONSET).total_seconds(),
    )


def test_object_change_interval_classification_is_not_point_time() -> None:
    early = temporal_evidence_snapshots(
        (_change_finding(ONSET + timedelta(minutes=10)),),
        _change_source(ONSET + timedelta(minutes=1), ONSET + timedelta(minutes=10)),
        ONSET,
        timedelta(minutes=15),
    )[0]
    late = temporal_evidence_snapshots(
        (_change_finding(ONSET + timedelta(minutes=30)),),
        _change_source(ONSET + timedelta(minutes=26), ONSET + timedelta(minutes=30)),
        ONSET,
        timedelta(minutes=15),
    )[0]
    straddling = temporal_evidence_snapshots(
        (_change_finding(ONSET + timedelta(minutes=30)),),
        _change_source(ONSET + timedelta(minutes=10), ONSET + timedelta(minutes=30)),
        ONSET,
        timedelta(minutes=15),
    )[0]
    assert early.interval_relation_to_onset_grace == "DEFINITELY_ONSET_CAPABLE"
    assert late.interval_relation_to_onset_grace == "DEFINITELY_LATE"
    assert straddling.interval_relation_to_onset_grace == "STRADDLES_ONSET_GRACE"


def test_verifier_effective_time_disagreement_is_visible() -> None:
    finding = _change_finding(ONSET + timedelta(minutes=30)).model_copy(
        update={"details": {"initiating_at": (ONSET + timedelta(minutes=10)).isoformat()}}
    )
    snapshot = temporal_evidence_snapshots(
        (finding,),
        _change_source(ONSET + timedelta(minutes=26), ONSET + timedelta(minutes=30)),
        ONSET,
        timedelta(minutes=15),
    )[0]
    assert snapshot.verifier_causal_at == (ONSET + timedelta(minutes=10)).isoformat()
    assert snapshot.role_verifier_time_disagreement


def _hypothesis(
    hypothesis_id: str,
    *,
    plausible: bool,
    reason_codes: tuple[str, ...] = (),
    gap: str | None = None,
) -> HypothesisTemporalAudit:
    return HypothesisTemporalAudit(
        hypothesis_id=hypothesis_id,
        causal_actor=f"shop/Deployment/{hypothesis_id}",
        members=(f"shop/Deployment/{hypothesis_id}",),
        causal_explanation="PATH",
        production_plausible=plausible,
        production_reason_codes=reason_codes,
        initiating_gap_category=gap,
        temporal_contradiction_certainties=(),
        production_verification_decision="VERIFIED",
        finding_keys=(),
    )


def test_unresolved_alternative_blocks_counterfactual_resolution() -> None:
    result = counterfactual_resolution(
        "E1_HARD_CONTRADICTIONS_ONLY",
        (
            _hypothesis("supported", plausible=True),
            _hypothesis(
                "uncertain",
                plausible=False,
                reason_codes=("NO_ONSET_CAPABLE_INITIATING_EVIDENCE",),
                gap="ONLY_SUPPORTING_EVIDENCE",
            ),
        ),
    )
    assert result.terminal_state == "AMBIGUOUS"
    assert result.supported_hypothesis_ids == ("supported",)
    assert result.unresolved_hypothesis_ids == ("uncertain",)


def test_all_unresolved_is_insufficient_and_e3_supports_only_supporting() -> None:
    hypotheses = (
        _hypothesis(
            "supporting",
            plausible=False,
            reason_codes=("NO_ONSET_CAPABLE_INITIATING_EVIDENCE",),
            gap="ONLY_SUPPORTING_EVIDENCE",
        ),
    )
    assert (
        counterfactual_resolution("E1_HARD_CONTRADICTIONS_ONLY", hypotheses).terminal_state
        == "INSUFFICIENT_EVIDENCE"
    )
    assert (
        counterfactual_resolution("E3_SUPPORTING_ONLY_PLAUSIBLE", hypotheses).terminal_state
        == "RESOLVED"
    )


def test_late_point_event_remains_a_hard_negative() -> None:
    # The production-path event is retained as point-time evidence; interval
    # uncertainty is only for object changes.
    entity = EntityRef.parse("shop/Pod/checkout")
    source = InMemorySource(
        name="late-event",
        event_items=[
            ClusterEvent(
                entity=entity,
                reason="Failed",
                first_at=ONSET + timedelta(minutes=30),
                last_at=ONSET + timedelta(minutes=30),
                evidence_id="event:late",
            )
        ],
    )
    finding = Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=entity,
        at=ONSET + timedelta(minutes=30),
        summary="late failure",
        evidence_ids=("event:late",),
    )
    snapshot = temporal_evidence_snapshots((finding,), source, ONSET, timedelta(minutes=15))[0]
    assert snapshot.temporal_source_class == "KUBERNETES_EVENT_TIME"
    assert snapshot.interval_relation_to_onset_grace == "NO_INTERVAL"


def test_counterfactuals_are_independent_of_order_names_and_verification() -> None:
    supported = _hypothesis("a", plausible=True)
    uncertain = _hypothesis(
        "b",
        plausible=False,
        reason_codes=("NO_ONSET_CAPABLE_INITIATING_EVIDENCE",),
        gap="ONLY_SUPPORTING_EVIDENCE",
    )
    baseline = counterfactual_resolution("E1_HARD_CONTRADICTIONS_ONLY", (supported, uncertain))
    renamed = (
        replace(uncertain, hypothesis_id="renamed-right", causal_actor="other/Pod/right"),
        replace(
            supported,
            hypothesis_id="renamed-left",
            causal_actor="other/Deployment/left",
            production_verification_decision="UNVERIFIED",
        ),
    )
    reordered = counterfactual_resolution("E1_HARD_CONTRADICTIONS_ONLY", renamed)
    assert baseline.terminal_state == reordered.terminal_state == "AMBIGUOUS"
    assert len(baseline.supported_hypothesis_ids) == len(reordered.supported_hypothesis_ids)
    assert len(baseline.unresolved_hypothesis_ids) == len(reordered.unresolved_hypothesis_ids)
