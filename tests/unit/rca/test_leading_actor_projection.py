"""Which causal leader the operator is shown: highest epistemic tier first, never the name order (roadmap C12)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from packages.rca.model import EntityRef
from packages.rca.presentation import project_leading_actor


def actor(name: str, kind: str = "Pod") -> EntityRef:
    return EntityRef(kind=kind, name=name, namespace="shop")


def hyp(
    hid: str, name: str, score: float = 8.0, kind: str = "Pod", window: bool | None = True
) -> Any:
    finding = SimpleNamespace(at=window)  # the stub window test reads this marker
    return SimpleNamespace(
        hypothesis_id=hid, causal_actor=actor(name, kind), score=score, findings=(finding,)
    )


def project(
    pool: list[Any], *, supported: tuple[str, ...] = (), strong: tuple[str, ...] = ()
) -> Any:
    return project_leading_actor(
        pool,
        supported=frozenset(supported),
        strong=frozenset(strong),
        in_window=lambda at: cast(bool | None, at),  # the stub's `at` is the window verdict itself
    )


def names(projection: Any) -> list[str]:
    return [ref.name for ref in projection.candidates]


def test_a_strong_claim_is_shown_alone_even_when_a_supported_one_ties_its_score() -> None:
    p = project([hyp("b", "b"), hyp("a", "a")], supported=("a", "b"), strong=("a",))
    assert (p.display, p.tier, names(p)) == ("SINGLE", "STRONG", ["a"])


def test_supported_claims_compete_and_an_unestablished_one_never_joins_them() -> None:
    pool = [
        hyp("c", "c"),
        hyp("a", "a"),
        hyp("b", "b"),
    ]  # c ranks first by name, as the engine would
    p = project(pool, supported=("a", "b"))
    assert (p.display, p.tier, names(p)) == ("COMPETING", "SUPPORTED", ["a", "b"])


def test_one_supported_claim_is_shown_whatever_ties_it_below() -> None:
    p = project([hyp("c", "c"), hyp("a", "a")], supported=("a",))
    assert (p.display, p.tier, names(p)) == ("SINGLE", "SUPPORTED", ["a"])


def test_tied_unestablished_candidates_are_not_established_and_listed() -> None:
    p = project([hyp("a", "a"), hyp("b", "b"), hyp("c", "c", score=5.0)])
    assert (p.display, p.tier, p.reason, names(p)) == (
        "NOT_ESTABLISHED",
        "UNESTABLISHED",
        "TIED_LEADERS",
        ["a", "b"],
    )


def test_a_single_unestablished_leader_with_evidence_in_the_window_is_shown_as_possible() -> None:
    p = project([hyp("a", "a"), hyp("b", "b", score=5.0)])
    assert (p.display, p.tier, p.reason, names(p)) == ("SINGLE", "UNESTABLISHED", None, ["a"])


def test_an_unestablished_leader_resting_only_on_evidence_outside_the_window_is_not_established() -> (
    None
):
    p = project([hyp("a", "a", window=False), hyp("b", "b", score=5.0)])
    assert (p.display, p.reason, names(p)) == (
        "NOT_ESTABLISHED",
        "NO_EVIDENCE_IN_INCIDENT_WINDOW",
        ["a"],
    )


def test_two_claims_on_one_actor_are_one_candidate() -> None:
    p = project(
        [hyp("s1", "sched", kind="Schedule"), hyp("s2", "sched", kind="Schedule")],
        supported=("s1", "s2"),
    )
    assert (p.display, names(p)) == ("SINGLE", ["sched"])


def test_an_empty_pool_names_no_one() -> None:
    p = project([])
    assert (p.display, p.tier, p.candidates) == ("NOT_ESTABLISHED", None, ())


def test_the_console_shows_competing_candidates_and_never_one_of_them_chosen_by_name() -> None:
    from apps.control_plane.console.mappers import presented_leader

    shown = presented_leader(
        "shop/Pod/c", "COMPETING", ("shop/StressChaos/a", "shop/StressChaos/b"), None
    )
    assert shown == ("COMPETING", ("shop/StressChaos/a", "shop/StressChaos/b"), None)
    assert (
        presented_leader("shop/Pod/c", "SINGLE", ("shop/StressChaos/a",), None)[2]
        == "shop/StressChaos/a"
    )
    # documents from before the projection keep their earlier presentation
    assert presented_leader("shop/Pod/c", None, (), None) == (
        "SINGLE",
        ("shop/Pod/c",),
        "shop/Pod/c",
    )
    assert presented_leader("shop/Pod/c", None, (), "NO_EVIDENCE_IN_INCIDENT_WINDOW")[2] is None


# ---- the engine's leader selection (m21-causal-semantics-contract.md, leader selection by tier) ------


def leader(
    pool: list[Any], *, supported: tuple[str, ...] = (), strong: tuple[str, ...] = ()
) -> str:
    from packages.rca.presentation import leader_by_tier

    return str(
        leader_by_tier(pool, supported=frozenset(supported), strong=frozenset(strong)).hypothesis_id
    )


def test_an_unsupported_candidate_never_leads_while_supported_ones_exist() -> None:
    # the pool is in ranking order: the unsupported pod ranks first by name on an equal score
    pool = [hyp("pod", "a-pod"), hyp("x1", "b-chaos"), hyp("x2", "c-chaos")]
    assert leader(pool, supported=("x1", "x2")) == "x1"


def test_a_strong_claim_leads_a_supported_one_ranked_ahead_of_it() -> None:
    pool = [hyp("s", "a", score=9.0), hyp("m", "b", score=8.0)]
    assert leader(pool, supported=("s", "m"), strong=("m",)) == "m"


def test_within_a_tier_the_ranking_order_decides() -> None:
    pool = [hyp("a", "a", score=9.0), hyp("b", "b", score=8.0)]
    assert leader(pool, supported=("a", "b")) == "a"
    assert leader(pool) == "a"  # no supported claim: the ranking alone, as before
