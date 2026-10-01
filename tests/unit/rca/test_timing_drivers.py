"""Why the diagnosis status differs under another admissible onset (audit only)."""

from __future__ import annotations

from rca_builders import at
from test_resolution import _hpa
from test_timing_assessment import uncertainty

from packages.rca.epistemic_digest import _timing_document
from packages.rca.model import Resolution, ResolutionTrace, StatusDriver, TimingAssessment
from packages.rca.timing_stability import (
    ELIMINATED,
    NOT_ELIMINATED,
    RELATION_D1,
    RELATION_ENDED_EPISODE,
    RELATION_TEMPORAL_CONTRADICTION,
    UNASSESSABLE,
    ClaimView,
    OnsetView,
    claim_views,
    compute_timing_assessment,
)

SUPPORTED, COMPETING = "SUPPORTED_CAUSE", "COMPETING_CAUSES"


def cv(standing: str, *, ended: str = NOT_ELIMINATED, temporal: str = NOT_ELIMINATED) -> ClaimView:
    return ClaimView(
        actor="shop/Pod/p",
        mechanism="MANIFESTATION_ONLY",
        evidence=frozenset({"e"}),
        relations={
            RELATION_D1: "NOT_FIRED",
            RELATION_ENDED_EPISODE: ended,
            RELATION_TEMPORAL_CONTRADICTION: temporal,
        },
        standing=standing,
    )


def assess(base: OnsetView, other: OnsetView) -> TimingAssessment:
    return compute_timing_assessment(uncertainty(10, 20), {at(10): base, at(20): other})


def drivers_at_20(base: OnsetView, other: OnsetView) -> tuple[StatusDriver, ...]:
    return assess(base, other).outcomes[1].drivers


def test_a_rule_that_could_not_be_judged_is_not_reported_as_a_contradiction() -> None:
    base = OnsetView(SUPPORTED, {"k": cv("NONE", ended=ELIMINATED)})
    other = OnsetView(COMPETING, {"k": cv("UNRESOLVED", ended=UNASSESSABLE)})
    assert drivers_at_20(base, other) == (
        StatusDriver(
            hypothesis_key="k",
            actor="shop/Pod/p",
            change="GAINS_COMPETITION",
            reason="BLOCKED_ENDED_EPISODE_RULE",
        ),
    )


def test_an_elimination_that_a_positive_outcome_no_longer_supports_is_named_as_such() -> None:
    base = OnsetView(SUPPORTED, {"k": cv("NONE", temporal=ELIMINATED)})
    other = OnsetView(COMPETING, {"k": cv("UNRESOLVED", temporal=NOT_ELIMINATED)})
    (driver,) = drivers_at_20(base, other)
    assert (driver.change, driver.reason) == ("GAINS_COMPETITION", "ELIMINATION_NOT_HOLDING")


def test_a_claim_that_forms_only_under_the_other_onset_is_named_as_such() -> None:
    base = OnsetView(SUPPORTED, {})
    other = OnsetView(COMPETING, {"k": cv("UNRESOLVED")})
    (driver,) = drivers_at_20(base, other)
    assert (driver.change, driver.reason) == ("GAINS_COMPETITION", "FORMS_ONLY_UNDER_THIS_ONSET")


def test_a_competitor_that_leaves_is_a_driver_too() -> None:
    base = OnsetView(COMPETING, {"gone": cv("UNRESOLVED"), "out": cv("UNRESOLVED")})
    other = OnsetView(SUPPORTED, {"out": cv("NONE", ended=ELIMINATED)})
    reasons = {d.hypothesis_key: (d.change, d.reason) for d in drivers_at_20(base, other)}
    assert reasons == {
        "gone": ("LEAVES_COMPETITION", "NO_LONGER_FORMED"),
        "out": ("LEAVES_COMPETITION", "ELIMINATED_OR_EXPLAINED_UNDER_THIS_ONSET"),
    }


def test_drivers_are_reported_only_where_the_status_differs() -> None:
    base = OnsetView(COMPETING, {"k": cv("NONE", ended=ELIMINATED)})
    same_status = OnsetView(COMPETING, {"k": cv("UNRESOLVED", ended=UNASSESSABLE)})
    result = assess(base, same_status)
    assert [o.drivers for o in result.outcomes] == [(), ()]


def test_claims_that_compete_under_both_onsets_or_under_neither_are_no_driver() -> None:
    base = OnsetView(SUPPORTED, {"a": cv("SUPPORTED"), "b": cv("NONE"), "c": cv("UNRESOLVED")})
    other = OnsetView(
        COMPETING, {"a": cv("SUPPORTED"), "b": cv("NONE"), "c": cv("SUPPORTED"), "d": cv("NONE")}
    )
    assert drivers_at_20(base, other) == ()


def test_the_onset_of_record_has_no_drivers_and_the_list_is_bounded_and_ordered() -> None:
    other = OnsetView(COMPETING, {f"k{i:02d}": cv("UNRESOLVED") for i in range(20)})
    result = assess(OnsetView(SUPPORTED, {}), other)
    assert result.outcomes[0].drivers == ()
    keys = [d.hypothesis_key for d in result.outcomes[1].drivers]
    assert keys == sorted(keys) and len(keys) == 12


def test_the_standing_of_a_claim_comes_from_the_trace_of_that_onset() -> None:
    supported, unresolved, other = (
        _hpa(name).model_copy(update={"hypothesis_key": f"k-{name}", "hypothesis_id": name})
        for name in ("s", "u", "n")
    )
    trace = ResolutionTrace(
        state=Resolution.AMBIGUOUS,
        plausible_hypotheses=("s",),
        unresolved_hypotheses=("u",),
    )
    views = claim_views([supported, unresolved, other], trace)
    assert {k: v.standing for k, v in views.items()} == {
        "k-s": "SUPPORTED",
        "k-u": "UNRESOLVED",
        "k-n": "NONE",
    }


def test_the_digest_carries_the_drivers() -> None:
    base = OnsetView(SUPPORTED, {"k": cv("NONE", ended=ELIMINATED)})
    other = OnsetView(COMPETING, {"k": cv("UNRESOLVED", ended=UNASSESSABLE)})
    outcomes = _timing_document(assess(base, other))["outcomes"]
    assert outcomes[1]["drivers"] == [  # type: ignore[index]
        {"key": "k", "change": "GAINS_COMPETITION", "reason": "BLOCKED_ENDED_EPISODE_RULE"}
    ]
    assert outcomes[0]["drivers"] == []  # type: ignore[index]
