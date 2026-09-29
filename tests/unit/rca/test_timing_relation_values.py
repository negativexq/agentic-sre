"""Three-valued relations: an unassessable outcome never counts as instability."""

from __future__ import annotations

from rca_builders import at
from test_resolution import _hpa
from test_timing_assessment import claim, uncertainty

from packages.rca.episode_end import RULE_ID as EPISODE_END_RULE_ID
from packages.rca.model import (
    Hypothesis,
    HypothesisResolutionAudit,
    HypothesisSignature,
    PreconditionAuditReason,
    PreconditionResult,
    PreconditionStatus,
    RelationTiming,
    Resolution,
    ResolutionElimination,
    ResolutionReasonCode,
    ResolutionTrace,
    RulePreconditionAudit,
    TimingAssessment,
    TimingStability,
)
from packages.rca.timing_stability import (
    ELIMINATED,
    NOT_ELIMINATED,
    RELATION_ENDED_EPISODE,
    UNASSESSABLE,
    ClaimView,
    OnsetView,
    claim_views,
    compute_timing_assessment,
    derive_timing_masks,
)

STABLE, SENSITIVE, UNASSESSED = (
    TimingStability.STABLE,
    TimingStability.SENSITIVE,
    TimingStability.UNASSESSED,
)


def ended(value: str) -> ClaimView:
    base = claim()
    return ClaimView(
        actor=base.actor,
        mechanism=base.mechanism,
        evidence=base.evidence,
        relations={**base.relations, RELATION_ENDED_EPISODE: value},
    )


def assess(*values: str) -> TimingAssessment:
    minutes = [10, 20, 30][: len(values)]
    return compute_timing_assessment(
        uncertainty(*minutes),
        {at(m): OnsetView("X", {"k": ended(v)}) for m, v in zip(minutes, values, strict=True)},
    )


def ended_relation(result: TimingAssessment) -> RelationTiming:
    return next(r for r in result.claims[0].relations if r.relation == RELATION_ENDED_EPISODE)


def test_an_unassessable_member_does_not_contradict_an_elimination() -> None:
    result = assess(ELIMINATED, UNASSESSABLE)
    relation = ended_relation(result)
    assert relation.stability is STABLE
    assert relation.values == (ELIMINATED, UNASSESSABLE)  # the audit still shows why
    assert result.claims[0].adjudication is STABLE


def test_two_assessed_values_are_still_sensitive_next_to_an_unassessable_one() -> None:
    result = assess(ELIMINATED, UNASSESSABLE, NOT_ELIMINATED)
    assert ended_relation(result).stability is SENSITIVE
    assert result.claims[0].adjudication is SENSITIVE


def test_a_relation_that_was_never_assessable_is_unassessed_not_stable_nor_sensitive() -> None:
    result = assess(UNASSESSABLE, UNASSESSABLE)
    assert ended_relation(result).stability is UNASSESSED
    assert result.claims[0].adjudication is STABLE


def test_a_stable_elimination_next_to_unassessable_members_is_not_withheld() -> None:
    result = assess(ELIMINATED, UNASSESSABLE, UNASSESSABLE)
    trace = ResolutionTrace(
        state=Resolution.AMBIGUOUS,
        eliminations=(
            ResolutionElimination(
                hypothesis_id="h",
                code=ResolutionReasonCode.MANIFESTATION_EPISODE_ENDED_BEFORE_ONSET,
            ),
        ),
    )
    claim_h = _hpa("h").model_copy(update={"hypothesis_key": "k", "hypothesis_id": "h"})
    assert not derive_timing_masks([claim_h], trace, result).any()


def audit_with(*items: RulePreconditionAudit) -> HypothesisResolutionAudit:
    return HypothesisResolutionAudit(
        hypothesis_id="h",
        signature=HypothesisSignature(),
        plausible=False,
        precondition_audit=items,
    )


def hypothesis() -> Hypothesis:
    return _hpa("h").model_copy(update={"hypothesis_key": "k", "hypothesis_id": "h"})


def ended_value(audit: HypothesisResolutionAudit | None, *, eliminated: bool = False) -> str:
    eliminations = (
        (
            ResolutionElimination(
                hypothesis_id="h",
                code=ResolutionReasonCode.MANIFESTATION_EPISODE_ENDED_BEFORE_ONSET,
            ),
        )
        if eliminated
        else ()
    )
    trace = ResolutionTrace(
        state=Resolution.AMBIGUOUS,
        eliminations=eliminations,
        hypothesis_audits=(audit,) if audit is not None else (),
    )
    return claim_views([hypothesis()], trace)["k"].relations[RELATION_ENDED_EPISODE]


def test_a_blocked_ended_episode_rule_is_unassessable_not_not_eliminated() -> None:
    for reason in (
        PreconditionAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE,
        PreconditionAuditReason.NO_DATA_AFTER_DEADLINE,
    ):
        blocked = audit_with(RulePreconditionAudit(rule_id=EPISODE_END_RULE_ID, reason=reason))
        assert ended_value(blocked) == UNASSESSABLE
    pending = audit_with(
        RulePreconditionAudit(
            rule_id=EPISODE_END_RULE_ID,
            result=PreconditionResult(status=PreconditionStatus.PENDING, not_before=at(0)),
        )
    )
    assert ended_value(pending) == UNASSESSABLE


def test_a_rule_whose_preconditions_passed_and_did_not_eliminate_is_not_eliminated() -> None:
    passed = audit_with(
        RulePreconditionAudit(
            rule_id=EPISODE_END_RULE_ID, result=PreconditionResult(status=PreconditionStatus.PASS)
        )
    )
    assert ended_value(passed) == NOT_ELIMINATED
    assert ended_value(None) == NOT_ELIMINATED
    assert ended_value(audit_with()) == NOT_ELIMINATED


def test_an_actual_elimination_is_eliminated_whatever_else_the_audit_says() -> None:
    other_rule = audit_with(
        RulePreconditionAudit(
            rule_id="some.other.rule",
            reason=PreconditionAuditReason.NO_DATA_AFTER_DEADLINE,
        )
    )
    assert ended_value(other_rule, eliminated=True) == ELIMINATED
    assert ended_value(other_rule) == NOT_ELIMINATED  # another rule's block is not this rule's
