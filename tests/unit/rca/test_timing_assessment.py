"""Formation and adjudication stability over the evidence-derived onset set."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rca_builders import at
from test_fault_execution import TARGET, _chaos_case, ev
from test_timing_stability import episode

from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.model import OnsetUncertainty, TimingStability
from packages.rca.source import InMemorySource
from packages.rca.timing_stability import (
    RELATION_D1,
    RELATION_ONSET,
    ClaimView,
    OnsetView,
    compute_timing_assessment,
    derive_onset_uncertainty,
)

W = at(0)
STABLE, SENSITIVE, UNASSESSED = (
    TimingStability.STABLE,
    TimingStability.SENSITIVE,
    TimingStability.UNASSESSED,
)


def uncertainty(*starts: float) -> OnsetUncertainty:
    episodes = [
        episode("RequestErrorRate", start, fingerprint=f"f{start}", firing=start == starts[-1])
        for start in starts
    ]
    return derive_onset_uncertainty(episodes, alert_observation_start=W, cutoff=at(90))


def claim(
    *,
    mechanism: str = "FAULT_INJECTION",
    evidence: tuple[str, ...] = ("e1",),
    d1: str = "FIRED",
    onset_relation: str = "initiating_before_onset",
) -> ClaimView:
    return ClaimView(
        actor="chaos/NetworkChaos/x",
        mechanism=mechanism,
        evidence=frozenset(evidence),
        relations={RELATION_D1: d1, RELATION_ONSET: onset_relation},
    )


def views(*items: tuple[float, str, dict[str, ClaimView]]) -> dict[datetime, OnsetView]:
    return {at(minute): OnsetView(status, claims) for minute, status, claims in items}


def test_a_single_admissible_onset_leaves_nothing_to_vary() -> None:
    result = compute_timing_assessment(
        uncertainty(10), views((10, "COMPETING_CAUSES", {"k": claim()}))
    )
    assert result.status is STABLE
    only = result.claims[0]
    assert (only.formation, only.adjudication) == (STABLE, STABLE)


def test_identical_claims_and_status_over_every_onset_are_stable() -> None:
    result = compute_timing_assessment(
        uncertainty(10, 20),
        views(
            (10, "COMPETING_CAUSES", {"k": claim()}),
            (20, "COMPETING_CAUSES", {"k": claim()}),
        ),
    )
    assert result.status is STABLE
    assert (result.claims[0].formation, result.claims[0].adjudication) == (STABLE, STABLE)


def test_a_claim_that_does_not_form_under_some_onset_is_formation_sensitive() -> None:
    result = compute_timing_assessment(
        uncertainty(10, 20),
        views((10, "X", {"k": claim()}), (20, "X", {})),
    )
    assert result.claims[0].formation is SENSITIVE


def test_a_different_evidence_set_or_assessed_mechanism_is_formation_sensitive() -> None:
    for changed in (claim(evidence=("e1", "e2")), claim(mechanism="MANIFESTATION_ONLY")):
        result = compute_timing_assessment(
            uncertainty(10, 20), views((10, "X", {"k": claim()}), (20, "X", {"k": changed}))
        )
        assert result.claims[0].formation is SENSITIVE


def test_a_relation_that_changes_value_is_adjudication_sensitive_and_records_why() -> None:
    result = compute_timing_assessment(
        uncertainty(10, 20),
        views(
            (10, "X", {"k": claim(d1="FIRED")}),
            (20, "X", {"k": claim(d1="NOT_FIRED")}),
        ),
    )
    only = result.claims[0]
    assert only.formation is STABLE and only.adjudication is SENSITIVE
    assert only.relation(RELATION_D1) is SENSITIVE
    assert only.relation(RELATION_ONSET) is STABLE
    d1 = next(r for r in only.relations if r.relation == RELATION_D1)
    assert d1.values == ("FIRED", "NOT_FIRED")


def test_the_diagnosis_status_stability_and_outcomes_are_reported_in_onset_order() -> None:
    result = compute_timing_assessment(
        uncertainty(10, 20, 30),
        views(
            (30, "SUPPORTED_CAUSE", {}),
            (10, "INSUFFICIENT_EVIDENCE", {}),
            (20, "COMPETING_CAUSES", {}),
        ),
    )
    assert result.status is SENSITIVE
    assert [o.diagnosis_status for o in result.outcomes] == [
        "INSUFFICIENT_EVIDENCE",
        "COMPETING_CAUSES",
        "SUPPORTED_CAUSE",
    ]


def test_nothing_is_claimed_without_an_onset_set_or_with_a_member_left_unevaluated() -> None:
    unassessable = derive_onset_uncertainty(None, alert_observation_start=W, cutoff=None)
    assert compute_timing_assessment(unassessable, {}).status is UNASSESSED
    partial = compute_timing_assessment(uncertainty(10, 20), views((10, "X", {"k": claim()})))
    assert partial.status is UNASSESSED and partial.claims == ()


def test_a_claim_absent_at_the_onset_of_record_is_not_reported() -> None:
    result = compute_timing_assessment(
        uncertainty(10, 20), views((10, "X", {}), (20, "X", {"late": claim()}))
    )
    assert result.claims == ()


EVENTS = [
    ev("chaos/Schedule/checkout-delay", "Spawned", 1, uid="s1"),
    ev("chaos/NetworkChaos/checkout-delay-x1y2z", "Applied", 1, uid="c1", message=TARGET),
    ev("chaos/NetworkChaos/checkout-delay-x1y2z", "Applied", 40, uid="c2", message=TARGET),
]


def source_with(*episodes: Any) -> InMemorySource:
    source = _chaos_case(EVENTS)  # the alert of record starts at minute 6
    source.alert_episode_items = list(episodes)
    return source


TWO_ONSETS = (
    episode("RequestLatency", 6, fingerprint="a"),
    episode("RequestLatency", 60, fingerprint="b", firing=True),
)


def test_the_engine_assesses_the_same_evidence_against_every_admissible_onset() -> None:
    case = build_case(source_with(*TWO_ONSETS))
    trace = diagnose_case(case).resolution_trace
    assert trace is not None and trace.timing is not None
    timing = trace.timing
    assert timing.uncertainty.h0 == at(6) and len(timing.uncertainty.members) == 2
    late = next(
        h
        for h in case.hypotheses
        if h.causal_actor.kind == "NetworkChaos"
        and h.actor_instance is not None
        and h.actor_instance.uid == "c2"
    )
    claim_timing = timing.claim(late.hypothesis_key)
    assert claim_timing is not None
    # Applied at minute 40 is a consequence of an onset at 6 but initiating before one at 60:
    # the claim's assessed mechanism, and so its formation, depends on where the onset is.
    assert claim_timing.formation is SENSITIVE


def test_without_capture_history_nothing_is_claimed() -> None:
    class NoCaptures:
        def __init__(self, base: InMemorySource) -> None:
            self._base = base

        def __getattr__(self, name: str) -> Any:
            if name == "alert_episodes":
                raise AttributeError(name)
            return getattr(self._base, name)

    case = build_case(NoCaptures(_chaos_case(EVENTS)))
    trace = diagnose_case(case).resolution_trace
    assert trace is not None and trace.timing is not None
    assert trace.timing.status is UNASSESSED
    assert trace.timing.uncertainty.reason == "NO_ALERT_CAPTURE_HISTORY"


def test_a_lone_episode_is_stable_by_definition() -> None:
    case = build_case(_chaos_case(EVENTS))
    trace = diagnose_case(case).resolution_trace
    assert trace is not None and trace.timing is not None
    assert trace.timing.status is STABLE
    assert all(c.formation is STABLE and c.adjudication is STABLE for c in trace.timing.claims)


def test_assessing_timing_changes_no_decision() -> None:
    source = source_with(*TWO_ONSETS)
    with_timing = diagnose_case(build_case(source)).resolution_trace
    without = diagnose_case(
        build_case(source), config=EngineConfig(timing_stability=False)
    ).resolution_trace
    assert with_timing is not None and without is not None
    assert without.timing is None
    assert with_timing.model_copy(update={"timing": None}) == without


def test_an_assessed_case_never_assesses_itself() -> None:
    case = build_case(source_with(*TWO_ONSETS), assessed_onset=at(60))
    trace = diagnose_case(case).resolution_trace
    assert trace is not None and trace.timing is None


def test_the_digest_is_deterministic_and_carries_how_the_onset_set_was_derived() -> None:
    def digest(*episodes: Any) -> str:
        return diagnosis_epistemic_digest(diagnose_case(build_case(source_with(*episodes))))

    assert digest(*TWO_ONSETS) == digest(*TWO_ONSETS)
    other = (
        episode("RequestLatency", 6, fingerprint="a"),
        episode("RequestLatency", 45, fingerprint="c", firing=True),
    )
    assert digest(*TWO_ONSETS) != digest(*other)
