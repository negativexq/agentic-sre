"""Onset-independent claim identity and family-level root-cause competition."""

from __future__ import annotations

from datetime import timedelta

from test_causal_claims import ONSET, claim, entity, finding
from test_fault_execution import TARGET, _chaos_case, ev

from packages.rca.demo import demo_source
from packages.rca.engine import build_case, diagnose
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.hypotheses import group_candidates
from packages.rca.model import (
    Candidate,
    EntityInstanceRef,
    FamilyState,
    FindingKind,
    Hypothesis,
    InstanceResolution,
    Symptoms,
)
from packages.rca.ranking import Context, RankingConfig
from packages.rca.resolution import resolve_hypotheses
from packages.rca.topology import Topology


def member(
    hypothesis_id: str, *, uid: str | None, supported: bool, family: str = "cfam:one"
) -> Hypothesis:
    """One exact claim of the logical actor ``cause``, observed on instance ``uid``."""
    actor = entity("cause")
    observed = finding(
        actor, kind=FindingKind.CONFIG_CHANGE if supported else FindingKind.FAILURE_EVENT, uid=uid
    )
    return claim("cause", supported=supported).model_copy(
        update={
            "hypothesis_id": hypothesis_id,
            "hypothesis_key": f"hkey:{hypothesis_id}",
            "causal_family_id": family,
            "mechanism_family": "CONFIG_CHANGE",
            "actor_instance": EntityInstanceRef(entity=actor, uid=uid) if uid else None,
            "findings": (observed,),
            "initiating_findings": (observed,) if supported else (),
        }
    )


def test_supported_and_unresolved_incarnations_do_not_compete_with_each_other() -> None:
    trace = resolve_hypotheses(
        (
            member("cause-a", uid="A", supported=True),
            member("cause-b", uid="B", supported=False),
        )
    )
    (family,) = trace.causal_families
    assert family.state is FamilyState.SUPPORTED
    assert family.instance_resolution is InstanceResolution.MULTIPLE_VIABLE
    assert (family.supported_members, family.unresolved_members) == (("cause-a",), ("cause-b",))
    # Exact members stay separately visible; only the competition count is per family.
    assert trace.plausible_hypotheses == ("cause-a",)
    assert trace.unresolved_hypotheses == ("cause-b",)
    assert trace.diagnosis_status == "SUPPORTED_CAUSE"


def test_different_families_still_compete() -> None:
    trace = resolve_hypotheses(
        (
            member("cause-a", uid="A", supported=True, family="cfam:one"),
            member("cause-b", uid="B", supported=False, family="cfam:two"),
        )
    )
    assert {f.state for f in trace.causal_families} == {
        FamilyState.SUPPORTED,
        FamilyState.UNRESOLVED,
    }
    assert trace.diagnosis_status == "COMPETING_CAUSES"


def test_two_supported_incarnations_are_one_supported_cause() -> None:
    a, b = member("cause-a", uid="A", supported=True), member("cause-b", uid="B", supported=True)
    trace = resolve_hypotheses((a, b))
    assert trace.plausible_hypotheses == ("cause-a", "cause-b")
    assert [f.state for f in trace.causal_families] == [FamilyState.SUPPORTED]
    assert trace.diagnosis_status == "SUPPORTED_CAUSE"
    assert trace.state.value != "RESOLVED"  # possible-cause tier: no strong authority


def test_unknown_instance_is_reported_not_inferred() -> None:
    trace = resolve_hypotheses((member("cause-a", uid=None, supported=True),))
    assert trace.causal_families[0].instance_resolution is InstanceResolution.UNKNOWN
    exact = resolve_hypotheses((member("cause-a", uid="A", supported=True),))
    assert exact.causal_families[0].instance_resolution is InstanceResolution.EXACT


def test_exact_members_are_kept_as_separate_audit_records() -> None:
    trace = resolve_hypotheses(
        (member("cause-a", uid="A", supported=True), member("cause-b", uid="B", supported=False))
    )
    audits = {a.hypothesis_id: a for a in trace.hypothesis_audits}
    assert set(audits) == {"cause-a", "cause-b"}
    assert {a.causal_family_id for a in audits.values()} == {"cfam:one"}
    assert audits["cause-a"].plausible and not audits["cause-b"].plausible


def test_claims_without_a_family_compete_as_themselves() -> None:
    trace = resolve_hypotheses((claim("x"), claim("y", supported=False)))
    assert len(trace.causal_families) == 2
    assert trace.diagnosis_status == "COMPETING_CAUSES"


def test_digest_does_not_depend_on_rank_order_representative() -> None:
    a, b = member("cause-a", uid="A", supported=True), member("cause-b", uid="B", supported=True)
    digests = set()
    for order in ((a, b), (b, a)):
        trace = resolve_hypotheses(order)
        diagnosis = diagnose(demo_source()).model_copy(
            update={"resolution_trace": trace, "alternative_hypotheses": order}
        )
        digests.add(diagnosis_epistemic_digest(diagnosis))
    assert len(digests) == 1


def _grouped(onset_shift: timedelta) -> tuple[Hypothesis, ...]:
    actor = entity("actor")
    onset = ONSET + onset_shift
    context = Context(
        symptoms=Symptoms(onset=onset, last_seen=onset, services=(), namespaces=(), alert_names=()),
        symptom_entities={actor},
        topology=Topology((), {}),
    )
    findings = (finding(actor, uid="u1", onset=onset),)
    return group_candidates(
        (Candidate(entity=actor, score=1, findings=findings),),
        Topology((), {}),
        context,
        RankingConfig(),
    ).hypotheses


def test_claim_and_family_identity_ignore_the_incident_onset() -> None:
    early, late = _grouped(timedelta(0)), _grouped(timedelta(hours=3))
    (h0,), (h1,) = early, late
    assert h0.episode_onset != h1.episode_onset
    assert (h0.hypothesis_key, h0.causal_family_id) == (h1.hypothesis_key, h1.causal_family_id)
    assert h0.mechanism_family == h1.mechanism_family == "CONFIG_CHANGE"


def test_instance_uid_is_part_of_claim_identity_but_not_of_family_identity() -> None:
    actor = entity("actor")
    context = Context(
        symptoms=Symptoms(onset=ONSET, last_seen=ONSET, services=(), namespaces=(), alert_names=()),
        symptom_entities={actor},
        topology=Topology((), {}),
    )
    findings = (finding(actor, uid="u1"), finding(actor, uid="u2"))
    result = group_candidates(
        (Candidate(entity=actor, score=1, findings=findings),),
        Topology((), {}),
        context,
        RankingConfig(),
    ).hypotheses
    assert len({h.hypothesis_key for h in result}) == 2
    assert len({h.causal_family_id for h in result}) == 1


def _chaos_claim_at(alert_minute: float) -> Hypothesis:
    events = [
        ev("chaos/Schedule/checkout-delay", "Spawned", 40, uid="s1"),
        ev("chaos/NetworkChaos/checkout-delay-x1y2z", "Applied", 40, uid="c1", message=TARGET),
    ]
    source = _chaos_case(events, alert_minute)
    case = build_case(source)
    return next(h for h in case.hypotheses if h.causal_actor.kind == "NetworkChaos")


def test_temporal_role_changes_the_assessed_mechanism_not_the_identity() -> None:
    initiating = _chaos_claim_at(60)  # fault applied before the onset
    consequence = _chaos_claim_at(6)  # fault applied long after the onset
    assert initiating.mechanism != consequence.mechanism
    assert initiating.mechanism_family == consequence.mechanism_family == "FAULT_INJECTION"
    assert initiating.hypothesis_key == consequence.hypothesis_key
    assert initiating.causal_family_id == consequence.causal_family_id


def test_schedule_incarnations_share_a_family_but_not_a_claim() -> None:
    events = [
        ev("chaos/Schedule/checkout-delay", "Spawned", 1, uid="s1"),
        ev("chaos/Schedule/checkout-delay", "Spawned", 40, uid="s2"),
        ev("chaos/NetworkChaos/checkout-delay-x1y2z", "Applied", 1, uid="c1", message=TARGET),
        ev("chaos/NetworkChaos/checkout-delay-x1y2z", "Applied", 40, uid="c2", message=TARGET),
    ]
    schedules = [
        h for h in build_case(_chaos_case(events)).hypotheses if h.causal_actor.kind == "Schedule"
    ]
    assert len(schedules) == 2
    assert len({h.hypothesis_key for h in schedules}) == 2
    assert len({h.causal_family_id for h in schedules}) == 1
