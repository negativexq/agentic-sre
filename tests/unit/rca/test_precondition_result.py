"""Generic tri-state precondition plumbing is neutral to RCA decision state."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.rca.episode_end import (
    RULE_ID as EPISODE_END_RULE_ID,
)
from packages.rca.episode_end import (
    EndedEpisode,
    EpisodeEndBasis,
    InstanceEpisodeEnd,
)
from packages.rca.epistemic_digest import diagnosis_epistemic_digest, epistemic_state
from packages.rca.model import (
    Confidence,
    Diagnosis,
    EliminationPrecondition,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    HypothesisEpistemicState,
    HypothesisResolutionAudit,
    HypothesisSignature,
    PreconditionAuditReason,
    PreconditionResult,
    PreconditionStatus,
    Resolution,
    ResolutionTrace,
    RulePreconditionAudit,
    Symptoms,
)
from packages.rca.resolution import preconditions_allow_elimination, resolve_hypotheses
from packages.rca.resource_mechanism import (
    RULE_ID as RESOURCE_RULE_ID,
)
from packages.rca.resource_mechanism import (
    MechanismMismatch,
    PodCoverage,
)
from packages.rca.root_cause_eligibility import RootCauseEligibilities

NOT_BEFORE = datetime(2026, 9, 26, 12, 15, tzinfo=UTC)


def _diagnosis() -> Diagnosis:
    audit = HypothesisResolutionAudit(
        hypothesis_id="h1",
        signature=HypothesisSignature(),
        epistemic_state=HypothesisEpistemicState.SUPPORTED,
        plausible=True,
    )
    return Diagnosis(
        incident_id="incident-1",
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        resolution=Resolution.AMBIGUOUS,
        summary="synthetic diagnosis",
        symptoms=Symptoms(
            onset=NOT_BEFORE,
            last_seen=NOT_BEFORE,
            services=(),
            namespaces=(),
            alert_names=(),
        ),
        resolution_trace=ResolutionTrace(
            state=Resolution.AMBIGUOUS,
            hypothesis_audits=(audit,),
        ),
    )


def _with_audit_metadata(
    diagnosis: Diagnosis,
    *,
    results: tuple[RulePreconditionAudit, ...] = (),
    reasons: tuple[RulePreconditionAudit, ...] = (),
) -> Diagnosis:
    assert diagnosis.resolution_trace is not None
    audit = diagnosis.resolution_trace.hypothesis_audits[0].model_copy(
        update={"precondition_audit": (*results, *reasons)}
    )
    trace = diagnosis.resolution_trace.model_copy(update={"hypothesis_audits": (audit,)})
    return diagnosis.model_copy(update={"resolution_trace": trace})


def test_pass_result_has_only_pass_status() -> None:
    result = PreconditionResult.passed()
    assert result.status is PreconditionStatus.PASS
    assert result.not_before is None
    assert result.reason is None


def test_pending_result_keeps_supplied_aware_boundary_without_clock_access() -> None:
    first = PreconditionResult.pending(NOT_BEFORE)
    second = PreconditionResult.pending(NOT_BEFORE)
    assert first == second
    assert first.status is PreconditionStatus.PENDING
    assert first.not_before == NOT_BEFORE
    assert first.reason is None


def test_disqualified_result_preserves_reason() -> None:
    result = PreconditionResult.disqualified("POSITIVE_SCOPE_MISMATCH")
    assert result.status is PreconditionStatus.DISQUALIFIED
    assert result.reason == "POSITIVE_SCOPE_MISMATCH"
    assert result.not_before is None


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "PASS", "not_before": NOT_BEFORE},
        {"status": "PASS", "reason": "not allowed"},
        {"status": "PENDING"},
        {"status": "PENDING", "not_before": NOT_BEFORE, "reason": "not allowed"},
        {"status": "DISQUALIFIED"},
        {"status": "DISQUALIFIED", "reason": "scope", "not_before": NOT_BEFORE},
        {"status": "DISQUALIFIED", "reason": ""},
        {"status": "DISQUALIFIED", "reason": "   "},
    ],
)
def test_invalid_result_shapes_fail_loudly(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PreconditionResult.model_validate(payload)


def test_pending_requires_timezone_aware_not_before() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        PreconditionResult.pending(datetime(2026, 9, 26, 12, 15))


@pytest.mark.parametrize(
    "result",
    [
        PreconditionResult.passed(),
        PreconditionResult.pending(NOT_BEFORE),
        PreconditionResult.disqualified("POSITIVE_SCOPE_MISMATCH"),
    ],
)
def test_result_json_roundtrip_is_deterministic(result: PreconditionResult) -> None:
    encoded = result.model_dump(mode="json")
    assert PreconditionResult.model_validate(encoded) == result
    assert result.model_dump_json() == PreconditionResult.model_validate(encoded).model_dump_json()


def test_generic_composition_only_allows_all_pass_results() -> None:
    assert preconditions_allow_elimination((PreconditionResult.passed(),))
    assert not preconditions_allow_elimination((PreconditionResult.pending(NOT_BEFORE),))
    assert not preconditions_allow_elimination(
        (PreconditionResult.disqualified("POSITIVE_SCOPE_MISMATCH"),)
    )
    # Mixed results remain available as individual facts; no priority is inferred.
    mixed = (
        PreconditionResult.pending(NOT_BEFORE),
        PreconditionResult.disqualified("POSITIVE_SCOPE_MISMATCH"),
    )
    assert not preconditions_allow_elimination(mixed)
    assert mixed[0].status is PreconditionStatus.PENDING
    assert mixed[1].status is PreconditionStatus.DISQUALIFIED


def _supported_hypothesis() -> Hypothesis:
    actor = EntityRef(namespace="shop", kind="Deployment", name="worker")
    target = EntityRef(namespace="shop", kind="Pod", name="worker-1")
    finding = Finding(
        kind=FindingKind.SPEC_CHANGE,
        entity=actor,
        at=NOT_BEFORE,
        incident_onset=NOT_BEFORE,
        onset_delta_seconds=-60,
        temporal_role=EvidenceTemporalRole.INITIATING,
        summary="resource settings changed",
        evidence_ids=("journal:1",),
    )
    return Hypothesis(
        hypothesis_id="h1",
        causal_actor=actor,
        members=(actor, target),
        findings=(finding,),
        initiating_findings=(finding,),
        causal_explanation="PATH",
    )


def _mechanism_mismatch(hypothesis: Hypothesis) -> MechanismMismatch:
    actor = hypothesis.causal_actor
    assert actor is not None
    pod = EntityRef(namespace="shop", kind="Pod", name="worker-1")
    coverage = PodCoverage(
        pod=pod,
        container="app",
        resource="memory",
        window_start=NOT_BEFORE,
        window_end=NOT_BEFORE,
        peak=0.2,
        evidence_id="read:1",
    )
    return MechanismMismatch(
        hypothesis_id=hypothesis.hypothesis_id,
        actor=actor,
        lowered=(("app", "memory"),),
        pods=(pod,),
        onset=NOT_BEFORE,
        boundary=NOT_BEFORE,
        window_start=NOT_BEFORE,
        window_end=NOT_BEFORE,
        coverage=(coverage,),
        change_evidence_ids=("journal:1",),
        preconditions=(EliminationPrecondition(name="normal", passed=True),),
    )


def test_pending_a2_precondition_prevents_elimination_without_state_change() -> None:
    hypothesis = _supported_hypothesis()
    mismatch = _mechanism_mismatch(hypothesis)
    before_rule = resolve_hypotheses((hypothesis,))
    with_rule = resolve_hypotheses(
        (hypothesis,), mechanism_mismatches={hypothesis.hypothesis_id: mismatch}
    )
    pending = resolve_hypotheses(
        (hypothesis,),
        mechanism_mismatches={hypothesis.hypothesis_id: mismatch},
        rule_preconditions={
            (hypothesis.hypothesis_id, RESOURCE_RULE_ID): (PreconditionResult.pending(NOT_BEFORE),)
        },
    )

    assert with_rule.eliminations
    assert pending.state is before_rule.state
    assert pending.eliminations == before_rule.eliminations == ()
    assert pending.eliminated_hypotheses == before_rule.eliminated_hypotheses == ()
    assert pending.plausible_hypotheses == before_rule.plausible_hypotheses
    (audit,) = pending.hypothesis_audits
    assert audit.epistemic_state is HypothesisEpistemicState.SUPPORTED
    assert audit.precondition_audit == (
        RulePreconditionAudit(
            rule_id=RESOURCE_RULE_ID,
            result=PreconditionResult.pending(NOT_BEFORE),
        ),
    )


def test_disqualified_a2_precondition_suppresses_only_its_rule_elimination() -> None:
    hypothesis = _supported_hypothesis()
    mismatch = _mechanism_mismatch(hypothesis)
    baseline = resolve_hypotheses((hypothesis,))
    blocked = resolve_hypotheses(
        (hypothesis,),
        mechanism_mismatches={hypothesis.hypothesis_id: mismatch},
        rule_preconditions={
            (hypothesis.hypothesis_id, RESOURCE_RULE_ID): (
                PreconditionResult.disqualified("POSITIVE_SCOPE_MISMATCH"),
            )
        },
    )
    assert blocked.state is baseline.state
    assert blocked.eliminations == baseline.eliminations == ()
    (audit,) = blocked.hypothesis_audits
    assert audit.precondition_audit[0].result is not None
    assert audit.precondition_audit[0].result.status is PreconditionStatus.DISQUALIFIED


def test_mixed_rule_results_are_preserved_without_aggregate_priority() -> None:
    hypothesis = _supported_hypothesis()
    mismatch = _mechanism_mismatch(hypothesis)
    results = (
        PreconditionResult.pending(NOT_BEFORE),
        PreconditionResult.disqualified("POSITIVE_SCOPE_MISMATCH"),
    )
    trace = resolve_hypotheses(
        (hypothesis,),
        mechanism_mismatches={hypothesis.hypothesis_id: mismatch},
        rule_preconditions={(hypothesis.hypothesis_id, RESOURCE_RULE_ID): results},
    )
    assert trace.eliminations == ()
    (audit,) = trace.hypothesis_audits
    assert tuple(item.result for item in audit.precondition_audit) == results


def test_pending_a1_precondition_does_not_make_hypothesis_root_ineligible() -> None:
    hypothesis = _supported_hypothesis()
    actor = hypothesis.causal_actor
    assert actor is not None
    ended = EndedEpisode(
        hypothesis_id=hypothesis.hypothesis_id,
        actor=actor,
        onset=NOT_BEFORE,
        boundary=NOT_BEFORE,
        instances=(
            InstanceEpisodeEnd(
                uid="uid-1",
                target="shop/Pod/worker-1@uid-1",
                basis=EpisodeEndBasis.RECOVERED,
                ended_at=NOT_BEFORE,
                observed_at=NOT_BEFORE,
                last_manifestation_at=NOT_BEFORE,
                end_evidence_id="lifecycle:1",
                decisive_evidence_ids=("lifecycle:1",),
            ),
        ),
        manifestation_evidence_ids=("event:1",),
        preconditions=(EliminationPrecondition(name="ended", passed=True),),
    )
    baseline = resolve_hypotheses((hypothesis,))
    with_rule = resolve_hypotheses(
        (hypothesis,),
        root_cause_eligibilities=RootCauseEligibilities((), {hypothesis.hypothesis_id: ended}),
    )
    pending = resolve_hypotheses(
        (hypothesis,),
        root_cause_eligibilities=RootCauseEligibilities((), {hypothesis.hypothesis_id: ended}),
        rule_preconditions={
            (hypothesis.hypothesis_id, EPISODE_END_RULE_ID): (
                PreconditionResult.pending(NOT_BEFORE),
            )
        },
    )

    assert with_rule.eliminations
    assert pending.state is baseline.state
    assert pending.eliminations == baseline.eliminations == ()
    assert pending.eliminated_hypotheses == baseline.eliminated_hypotheses == ()
    assert pending.plausible_hypotheses == baseline.plausible_hypotheses


def test_pending_audit_metadata_does_not_change_epistemic_decision_state() -> None:
    baseline = _diagnosis()
    pending = _with_audit_metadata(
        baseline,
        results=(
            RulePreconditionAudit(
                rule_id="m16.resource-pressure",
                result=PreconditionResult.pending(NOT_BEFORE),
            ),
        ),
    )

    assert epistemic_state(baseline) == epistemic_state(pending)
    assert diagnosis_epistemic_digest(baseline) == diagnosis_epistemic_digest(pending)
    assert pending.resolution == baseline.resolution
    assert pending.root_cause == baseline.root_cause
    assert pending.resolution_trace is not None and baseline.resolution_trace is not None
    baseline_audit = baseline.resolution_trace.hypothesis_audits[0]
    pending_audit = pending.resolution_trace.hypothesis_audits[0]
    assert pending_audit.epistemic_state is baseline_audit.epistemic_state
    assert pending_audit.plausible == baseline_audit.plausible
    assert pending.resolution_trace.eliminations == baseline.resolution_trace.eliminations
    assert pending_audit.precondition_audit == (
        RulePreconditionAudit(
            rule_id="m16.resource-pressure",
            result=PreconditionResult.pending(NOT_BEFORE),
        ),
    )


@pytest.mark.parametrize(
    "reason",
    [
        PreconditionAuditReason.NO_DATA_AFTER_DEADLINE,
        PreconditionAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE,
    ],
)
def test_deadline_missing_reasons_are_audit_only_and_neutral(
    reason: PreconditionAuditReason,
) -> None:
    baseline = _diagnosis()
    audited = _with_audit_metadata(
        baseline,
        reasons=(RulePreconditionAudit(rule_id=RESOURCE_RULE_ID, reason=reason),),
    )

    assert audited.resolution == baseline.resolution
    assert audited.root_cause == baseline.root_cause
    assert audited.resolution_trace is not None
    assert audited.resolution_trace.eliminations == ()
    audit = audited.resolution_trace.hypothesis_audits[0]
    assert audit.epistemic_state is HypothesisEpistemicState.SUPPORTED
    assert audit.precondition_audit == (
        RulePreconditionAudit(rule_id=RESOURCE_RULE_ID, reason=reason),
    )
    assert diagnosis_epistemic_digest(audited) == diagnosis_epistemic_digest(baseline)


@pytest.mark.parametrize(
    "reason",
    [
        PreconditionAuditReason.NO_DATA_AFTER_DEADLINE,
        PreconditionAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE,
    ],
)
def test_resolution_carries_deadline_reason_without_decision_effect(
    reason: PreconditionAuditReason,
) -> None:
    hypothesis = _supported_hypothesis()
    mismatch = _mechanism_mismatch(hypothesis)
    baseline = resolve_hypotheses((hypothesis,))
    audited = resolve_hypotheses(
        (hypothesis,),
        mechanism_mismatches={hypothesis.hypothesis_id: mismatch},
        precondition_reasons={(hypothesis.hypothesis_id, RESOURCE_RULE_ID): (reason,)},
    )
    with_rule = resolve_hypotheses(
        (hypothesis,), mechanism_mismatches={hypothesis.hypothesis_id: mismatch}
    )
    assert with_rule.eliminations
    assert audited.state is baseline.state
    assert audited.eliminations == baseline.eliminations == ()
    (audit,) = audited.hypothesis_audits
    assert audit.precondition_audit == (
        RulePreconditionAudit(rule_id=RESOURCE_RULE_ID, reason=reason),
    )
    assert audit.epistemic_state is HypothesisEpistemicState.SUPPORTED


def test_pre_5_1_diagnosis_documents_and_boolean_elimination_audits_still_parse() -> None:
    diagnosis = _diagnosis().model_dump(mode="json")
    trace = diagnosis["resolution_trace"]
    assert isinstance(trace, dict)
    audit = trace["hypothesis_audits"][0]
    assert isinstance(audit, dict)
    audit.pop("precondition_audit", None)
    trace["eliminations"] = [
        {
            "hypothesis_id": "h1",
            "code": "EXPLICIT_TEMPORAL_CONTRADICTION",
            "preconditions": [{"name": "legacy", "passed": True, "detail": ""}],
        }
    ]

    restored = Diagnosis.model_validate(diagnosis)
    assert restored.resolution_trace is not None
    assert restored.resolution_trace.hypothesis_audits[0].precondition_audit == ()
    assert restored.resolution_trace.eliminations[0].preconditions == (
        EliminationPrecondition(name="legacy", passed=True, detail=""),
    )
