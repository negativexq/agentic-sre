"""A1 RECOVERED maturity and continuity use the M19-5.1 tri-state contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.rca.episode_end import (
    RULE_ID,
    EpisodeEndBasis,
    EpisodeEndDisqualificationReason,
    assess_ended_episode,
    evaluate_ended_episodes,
)
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.model import (
    Confidence,
    Diagnosis,
    EntityInstanceRef,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    HypothesisEpistemicState,
    Lifecycle,
    ObjectVersion,
    PodStatusObservation,
    PreconditionAuditReason,
    PreconditionStatus,
    Resolution,
    ResolutionTrace,
    Symptoms,
)
from packages.rca.resolution import resolve_hypotheses

ONSET = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
GRACE = timedelta(minutes=15)
BOUNDARY = ONSET + GRACE
POD = EntityRef(namespace="shop", kind="Pod", name="worker-0")


def _finding(uid: str | None = "u1", minute: float = -10) -> Finding:
    return Finding(
        kind=FindingKind.FAILURE_EVENT,
        entity=POD,
        entity_instance=EntityInstanceRef(entity=POD, uid=uid) if uid else None,
        at=ONSET + timedelta(minutes=minute),
        incident_onset=ONSET,
        temporal_role=EvidenceTemporalRole.AMBIGUOUS,
        summary="readiness probe failed",
        evidence_ids=(f"event:{uid}:{minute}",),
    )


def _hypothesis(*findings: Finding) -> Hypothesis:
    return Hypothesis(
        hypothesis_id="hypothesis:worker",
        causal_actor=POD,
        members=(POD,),
        findings=findings,
        supporting_findings=findings,
        causal_explanation="DIRECT",
    )


def _status(
    uid: str,
    minute: float,
    *,
    ready: bool | None = True,
    since: float | None = -5,
    evidence: str | None = None,
) -> PodStatusObservation:
    return PodStatusObservation(
        pod=POD,
        uid=uid,
        observed_at=ONSET + timedelta(minutes=minute),
        ready=ready,
        ready_since=ONSET + timedelta(minutes=since) if since is not None else None,
        evidence_id=evidence or f"status:{uid}:{minute}",
    )


def _deleted(uid: str) -> ObjectVersion:
    body: dict[str, Any] = {"kind": "Pod", "metadata": {"name": POD.name, "uid": uid}}
    return ObjectVersion(
        entity=POD,
        uid=uid,
        observed_at=ONSET - timedelta(minutes=2),
        body=body,
        evidence_id=f"journal:{uid}:deleted",
        lifecycle=Lifecycle.DELETED,
    )


def _evaluation(
    *findings: Finding,
    statuses: tuple[PodStatusObservation, ...] = (),
    history: tuple[ObjectVersion, ...] = (),
    evaluation_at: datetime | None = None,
) -> Any:
    return evaluate_ended_episodes(
        (_hypothesis(*findings),),
        history={POD: history} if history else {},
        pod_statuses=statuses,
        onset=ONSET,
        grace=GRACE,
        evaluation_at=evaluation_at,
    )


def test_recovered_pass_and_exact_grace_boundary_remain_inclusive() -> None:
    for minute in (15, 20):
        ended = assess_ended_episode(
            _hypothesis(_finding()),
            history={},
            pod_statuses=(_status("u1", minute),),
            onset=ONSET,
            grace=GRACE,
        )
        assert ended is not None
        assert ended.basis is EpisodeEndBasis.RECOVERED
        assert ended.instances[0].observed_at == ONSET + timedelta(minutes=minute)
        assert ended.instances[0].decisive_evidence_ids == (f"status:u1:{minute}",)


def test_valid_same_uid_candidate_before_grace_is_pending_not_decisive() -> None:
    result = _evaluation(_finding(), statuses=(_status("u1", 8),))
    (pending,) = result.preconditions["hypothesis:worker"]
    assert pending.status is PreconditionStatus.PENDING
    assert pending.not_before == BOUNDARY
    assert result.ended_episodes == {}
    assert result.reasons == {}


def test_pre_onset_ready_status_alone_does_not_create_pending() -> None:
    result = _evaluation(_finding(), statuses=(_status("u1", -2),))
    assert result.preconditions == {}
    assert result.reasons == {}


def test_pending_is_audited_and_does_not_change_epistemic_state_or_digest() -> None:
    hypothesis = _hypothesis(_finding())
    evaluated = _evaluation(_finding(), statuses=(_status("u1", 8),))
    pending = evaluated.preconditions[hypothesis.hypothesis_id]
    baseline_trace = resolve_hypotheses((hypothesis,))
    pending_trace = resolve_hypotheses(
        (hypothesis,),
        rule_preconditions={(hypothesis.hypothesis_id, RULE_ID): pending},
    )

    def diagnosis(trace: ResolutionTrace) -> Diagnosis:
        return Diagnosis(
            incident_id="synthetic",
            root_cause=None,
            confidence=Confidence.UNVERIFIED,
            resolution=trace.state,
            summary="synthetic",
            symptoms=Symptoms(
                onset=ONSET,
                last_seen=ONSET,
                services=(),
                namespaces=(),
                alert_names=(),
            ),
            hypothesis=hypothesis,
            resolution_trace=trace,
        )

    before, after = diagnosis(baseline_trace), diagnosis(pending_trace)
    (audit,) = pending_trace.hypothesis_audits
    assert audit.precondition_audit[0].result is not None
    assert audit.precondition_audit[0].result.status is PreconditionStatus.PENDING
    assert pending_trace.state == baseline_trace.state
    assert pending_trace.eliminations == baseline_trace.eliminations == ()
    assert pending_trace.eliminated_hypotheses == baseline_trace.eliminated_hypotheses == ()
    assert audit.epistemic_state is HypothesisEpistemicState.UNRESOLVED
    assert audit.plausible == baseline_trace.hypothesis_audits[0].plausible
    assert diagnosis_epistemic_digest(after) == diagnosis_epistemic_digest(before)


def test_same_uid_post_onset_ready_false_disqualifies_without_contradiction() -> None:
    result = _evaluation(_finding(), statuses=(_status("u1", 4, ready=False, since=None),))
    (blocked,) = result.preconditions["hypothesis:worker"]
    assert blocked.status is PreconditionStatus.DISQUALIFIED
    assert blocked.reason == EpisodeEndDisqualificationReason.POST_ONSET_READY_FALSE.value

    trace = resolve_hypotheses(
        (_hypothesis(_finding()),),
        rule_preconditions={("hypothesis:worker", RULE_ID): (blocked,)},
    )
    assert trace.eliminations == ()
    assert trace.eliminated_hypotheses == ()
    assert trace.state is Resolution.INSUFFICIENT_EVIDENCE
    assert trace.hypothesis_audits[0].epistemic_state is HypothesisEpistemicState.UNRESOLVED


def test_ready_false_at_onset_is_inclusive_for_continuity_break() -> None:
    result = _evaluation(_finding(), statuses=(_status("u1", 0, ready=False, since=None),))
    (blocked,) = result.preconditions["hypothesis:worker"]
    assert blocked.reason == EpisodeEndDisqualificationReason.POST_ONSET_READY_FALSE.value


def test_ready_transition_after_onset_is_a_positive_continuity_break() -> None:
    result = _evaluation(_finding(), statuses=(_status("u1", 8, since=2),))
    (blocked,) = result.preconditions["hypothesis:worker"]
    assert blocked.status is PreconditionStatus.DISQUALIFIED
    assert blocked.reason == EpisodeEndDisqualificationReason.RECOVERY_CONTINUITY_BROKEN.value


def test_disagreeing_same_uid_status_breaks_candidate_continuity() -> None:
    result = _evaluation(
        _finding(),
        statuses=(_status("u1", 8), _status("u1", 6, ready=False, since=5)),
    )
    (blocked,) = result.preconditions["hypothesis:worker"]
    assert blocked.status is PreconditionStatus.DISQUALIFIED
    assert blocked.reason == EpisodeEndDisqualificationReason.POST_ONSET_READY_FALSE.value


def test_conflicting_ready_transition_breaks_existing_continuity_predicate() -> None:
    result = _evaluation(
        _finding(),
        statuses=(
            _status("u1", 6, since=-7),
            _status("u1", 8, since=-5),
        ),
    )
    (blocked,) = result.preconditions["hypothesis:worker"]
    assert blocked.status is PreconditionStatus.DISQUALIFIED
    assert blocked.reason == EpisodeEndDisqualificationReason.RECOVERY_CONTINUITY_BROKEN.value


def test_wrong_uid_cannot_mature_or_disqualify_exact_instance() -> None:
    pending = _evaluation(_finding("u1"), statuses=(_status("u2", 8),))
    assert pending.preconditions == {}
    assert pending.ended_episodes == {}

    false_other_uid = _evaluation(
        _finding("u1"), statuses=(_status("u2", 20, ready=False, since=None),)
    )
    assert false_other_uid.preconditions == {}
    assert false_other_uid.ended_episodes == {}


def test_uidless_manifestation_remains_inapplicable() -> None:
    result = _evaluation(
        _finding(None), statuses=(_status("u1", 8),), evaluation_at=BOUNDARY + GRACE
    )
    assert result.ended_episodes == {}
    assert result.preconditions == {}
    assert result.reasons == {}


def test_terminated_path_wins_over_pending_recovery_candidate() -> None:
    result = _evaluation(
        _finding(),
        statuses=(_status("u1", -4),),
        history=(_deleted("u1"),),
    )
    ended = result.ended_episodes["hypothesis:worker"]
    assert ended.basis is EpisodeEndBasis.TERMINATED
    assert result.preconditions == {}
    assert result.reasons == {}


def test_multiple_instances_keep_each_uid_outcome_and_block_partial_end() -> None:
    result = _evaluation(
        _finding("u1"),
        _finding("u2", -8),
        history=(_deleted("u1"),),
        statuses=(_status("u2", 8),),
    )
    assert result.ended_episodes == {}
    (pending,) = result.preconditions["hypothesis:worker"]
    assert pending.status is PreconditionStatus.PENDING
    assert pending.not_before == BOUNDARY


def test_deadline_missing_and_partial_status_are_neutral_audit_reasons() -> None:
    no_status = _evaluation(_finding(), evaluation_at=BOUNDARY + timedelta(seconds=1))
    assert no_status.reasons["hypothesis:worker"] == (
        PreconditionAuditReason.NO_DATA_AFTER_DEADLINE,
    )
    partial = _evaluation(
        _finding(),
        statuses=(_status("u1", 8),),
        evaluation_at=BOUNDARY + timedelta(seconds=1),
    )
    assert partial.reasons["hypothesis:worker"] == (
        PreconditionAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE,
    )

    trace = resolve_hypotheses(
        (_hypothesis(_finding()),),
        precondition_reasons={("hypothesis:worker", RULE_ID): partial.reasons["hypothesis:worker"]},
    )
    assert trace.eliminations == ()
    assert trace.hypothesis_audits[0].epistemic_state is HypothesisEpistemicState.UNRESOLVED


def test_evaluation_at_maturity_boundary_records_missing_as_deadline_reason() -> None:
    result = _evaluation(_finding(), evaluation_at=BOUNDARY)
    assert result.reasons["hypothesis:worker"] == (PreconditionAuditReason.NO_DATA_AFTER_DEADLINE,)


def test_later_reincarnation_cannot_complete_pending_exact_uid() -> None:
    result = _evaluation(
        _finding("u1"),
        statuses=(_status("u1", 8), _status("u2", 20)),
    )
    (pending,) = result.preconditions["hypothesis:worker"]
    assert pending.status is PreconditionStatus.PENDING
    assert result.ended_episodes == {}
