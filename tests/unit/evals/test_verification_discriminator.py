"""Ground-truth-blind tests for the frozen P2B-0 rules."""

from __future__ import annotations

from datetime import UTC, datetime

from packages.evals.verification_discriminator import VerificationRuleResult, evaluate_rules
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


def _hypothesis(name: str, score: float = 1.0) -> Hypothesis:
    entity = EntityRef.parse(f"ns/Deployment/{name}")
    finding = Finding(
        kind=FindingKind.CONFIG_CHANGE,
        entity=entity,
        at=datetime(2026, 1, 1, tzinfo=UTC),
        summary="changed",
        evidence_ids=(f"raw:{name}",),
        temporal_role=EvidenceTemporalRole.INITIATING,
    )
    return Hypothesis(
        hypothesis_id=f"h:{name}",
        causal_actor=entity,
        findings=(finding,),
        initiating_findings=(finding,),
        causal_explanation="DIRECT",
        score=score,
    )


def _trace(
    hypothesis: Hypothesis,
    decision: Confidence,
    *failures: str,
) -> VerificationTrace:
    return VerificationTrace(
        candidate=hypothesis.causal_actor,
        decision=decision,
        predicates=tuple(
            VerificationPredicate(name=name, status=PredicateStatus.FAIL) for name in failures
        ),
    )


def _results(
    left: Confidence,
    right: Confidence,
    *right_failures: str,
) -> tuple[VerificationRuleResult, ...]:
    hypotheses = (_hypothesis("left"), _hypothesis("right"))
    traces = {
        hypotheses[0].hypothesis_id: _trace(hypotheses[0], left),
        hypotheses[1].hypothesis_id: _trace(hypotheses[1], right, *right_failures),
    }
    return evaluate_rules("AMBIGUOUS", hypotheses, traces)


def test_unique_verified_and_frozen_predicate_rules() -> None:
    v1, v2, v3 = _results(
        Confidence.VERIFIED,
        Confidence.UNVERIFIED,
        "late_change_contradiction",
    )
    assert v1.triggered and v1.selected_actor == "ns/Deployment/left"
    assert v2.triggered
    assert v3.triggered


def test_two_verified_hypotheses_abstain() -> None:
    results = _results(Confidence.VERIFIED, Confidence.VERIFIED)
    assert all(not result.triggered for result in results)
    assert all(result.abstention_reason == "MULTIPLE_VERIFIED_HYPOTHESES" for result in results)


def test_no_verified_hypothesis_abstains_without_ranking() -> None:
    results = _results(Confidence.LIKELY, Confidence.UNVERIFIED)
    assert all(not result.triggered for result in results)
    assert all(result.abstention_reason == "NO_VERIFIED_HYPOTHESIS" for result in results)


def test_v2_requires_hard_failure_while_v3_accepts_onset_failure() -> None:
    v1, v2, v3 = _results(
        Confidence.VERIFIED,
        Confidence.UNVERIFIED,
        "initiating_evidence_near_onset",
    )
    assert v1.triggered
    assert not v2.triggered
    assert v2.abstention_reason == "COMPETITOR_HAS_NO_HARD_FAILURE"
    assert v3.triggered


def test_missing_trace_fails_closed() -> None:
    hypotheses = (_hypothesis("left"), _hypothesis("right"))
    traces = {hypotheses[0].hypothesis_id: _trace(hypotheses[0], Confidence.VERIFIED)}
    results = evaluate_rules("AMBIGUOUS", hypotheses, traces)
    assert all(not result.triggered for result in results)
    assert all(result.abstention_reason == "MISSING_VERIFICATION_TRACE" for result in results)


def test_score_perturbation_and_hypothesis_order_do_not_change_rules() -> None:
    left, right = _hypothesis("left", score=1.0), _hypothesis("right", score=2.0)
    traces = {
        left.hypothesis_id: _trace(left, Confidence.VERIFIED),
        right.hypothesis_id: _trace(right, Confidence.UNVERIFIED, "candidate_linked_to_symptom"),
    }
    baseline = evaluate_rules("AMBIGUOUS", (left, right), traces)
    changed = evaluate_rules(
        "AMBIGUOUS",
        (right.model_copy(update={"score": 1000.0}), left.model_copy(update={"score": -10.0})),
        traces,
    )
    assert baseline == changed
