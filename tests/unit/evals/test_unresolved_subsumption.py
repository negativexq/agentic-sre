from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from packages.evals.unresolved_subsumption import (
    RULE_IDS,
    _counterfactual,
    _pair_eligibility,
    blocker_class,
)
from packages.rca.model import (
    CausalHop,
    Edge,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
)
from packages.rca.topology import Topology


def _finding(
    kind: FindingKind, entity: EntityRef, at: int, *, role: EvidenceTemporalRole
) -> Finding:
    return Finding(
        kind=kind,
        entity=entity,
        at=datetime(2025, 1, 1, 0, 0, at, tzinfo=UTC),
        summary=kind.value,
        temporal_role=role,
        incident_onset=datetime(2025, 1, 1, tzinfo=UTC),
    )


def _pair(
    *, member: bool = False, stored_path: bool = True, reverse: bool = False
) -> tuple[Hypothesis, Hypothesis, SimpleNamespace]:
    source = EntityRef(namespace="ns", kind="ConfigMap", name="source")
    target = EntityRef(namespace="ns", kind="Pod", name="target")
    early = _finding(FindingKind.CONFIG_CHANGE, source, 10, role=EvidenceTemporalRole.INITIATING)
    manifestation = _finding(
        FindingKind.CONTAINER_FAILURE, target, 20, role=EvidenceTemporalRole.SUPPORTING
    )
    edges = [Edge(source=source, target=target, relation="disrupts")]
    if reverse:
        edges.append(Edge(source=target, target=source, relation="disrupts"))
    path = (CausalHop(source=source, relation="disrupts", target=target),)
    supported = Hypothesis(
        hypothesis_id="supported",
        causal_actor=source,
        manifestations=(target,) if member else (),
        findings=(early,),
        initiating_findings=(early,),
        causal_paths=(path,) if stored_path else (),
        linked_symptoms=("checkout",),
        causal_explanation="PATH",
    )
    unresolved = Hypothesis(
        hypothesis_id="unresolved",
        causal_actor=target,
        findings=(manifestation,),
        supporting_findings=(manifestation,),
        linked_symptoms=("checkout",),
        causal_explanation="PATH",
    )
    case = SimpleNamespace(topology=Topology(edges, {}))
    return supported, unresolved, case


def test_manifestation_only_classification_does_not_use_actor_kind() -> None:
    source = EntityRef(namespace="ns", kind="ConfigMap", name="source")
    finding = _finding(FindingKind.FAILURE_EVENT, source, 20, role=EvidenceTemporalRole.CONSEQUENCE)
    hypothesis = Hypothesis(
        hypothesis_id="h",
        causal_actor=source,
        findings=(finding,),
        supporting_findings=(finding,),
        causal_explanation="PATH",
    )
    assert blocker_class(hypothesis) == "MANIFESTATION_ONLY"


def test_s1_requires_existing_episode_membership() -> None:
    supported, unresolved, case = _pair(member=True)
    pair = _pair_eligibility(supported, unresolved, case)
    assert pair.s1_eligible


def test_s2_requires_stored_path_and_s3_can_use_broader_path() -> None:
    supported, unresolved, case = _pair(stored_path=False)
    pair = _pair_eligibility(supported, unresolved, case)
    assert not pair.s2_eligible
    assert pair.s3_eligible


def test_reverse_path_and_missing_time_fail_closed() -> None:
    supported, unresolved, case = _pair(reverse=True)
    pair = _pair_eligibility(supported, unresolved, case)
    assert not pair.s2_eligible
    assert not pair.s3_eligible


def test_temporal_inversion_and_missing_shared_path_fail_closed() -> None:
    supported, unresolved, case = _pair()
    later = _finding(
        FindingKind.CONFIG_CHANGE,
        supported.causal_actor,
        30,
        role=EvidenceTemporalRole.INITIATING,
    )
    supported = supported.model_copy(update={"findings": (later,), "initiating_findings": (later,)})
    pair = _pair_eligibility(supported, unresolved, case)
    assert pair.temporal_order == "MANIFESTATION_PRECEDES_SUPPORTED"
    assert not pair.s2_eligible and not pair.s3_eligible

    supported, unresolved, case = _pair()
    case = SimpleNamespace(topology=Topology([], {}))
    supported = supported.model_copy(update={"causal_paths": ()})
    pair = _pair_eligibility(supported, unresolved, case)
    assert pair.shared_linked_symptoms
    assert not pair.forward_path
    assert not pair.s2_eligible and not pair.s3_eligible

    supported, unresolved, case = _pair()
    unresolved = unresolved.model_copy(
        update={"findings": (unresolved.findings[0].model_copy(update={"at": None}),)}
    )
    pair = _pair_eligibility(supported, unresolved, case)
    assert not pair.s2_eligible
    assert not pair.s3_eligible


def test_independent_initiating_evidence_is_never_subsumable() -> None:
    supported, unresolved, case = _pair()
    initiating = _finding(
        FindingKind.CONFIG_CHANGE,
        unresolved.causal_actor,
        5,
        role=EvidenceTemporalRole.INITIATING,
    )
    unresolved = unresolved.model_copy(
        update={
            "findings": (*unresolved.findings, initiating),
            "initiating_findings": (initiating,),
        }
    )
    pair = _pair_eligibility(supported, unresolved, case)
    assert pair.independent_initiating_evidence
    assert not pair.s1_eligible and not pair.s2_eligible and not pair.s3_eligible


def test_unresolved_remains_a_blocker_in_counterfactual() -> None:
    supported, unresolved, _ = _pair()
    diagnosis = SimpleNamespace(resolution_trace=SimpleNamespace(dominance_relations=()))
    result = _counterfactual(RULE_IDS[0], (supported,), (unresolved,), (), set(), diagnosis)
    assert result.counterfactual_resolution == "AMBIGUOUS"
    assert result.unresolved_ids == ("unresolved",)
    assert result.subsumed_ids == ()


def test_two_supported_episodes_remain_ambiguous_without_dominance() -> None:
    supported, unresolved, _ = _pair()
    second = supported.model_copy(update={"hypothesis_id": "supported-2"})
    diagnosis = SimpleNamespace(resolution_trace=SimpleNamespace(dominance_relations=()))
    result = _counterfactual(
        RULE_IDS[0], (supported, second), (unresolved,), (), {"unresolved"}, diagnosis
    )
    assert result.counterfactual_resolution == "AMBIGUOUS"


def test_scores_and_verification_are_not_inputs_to_pair_eligibility() -> None:
    supported, unresolved, case = _pair()
    baseline = _pair_eligibility(supported, unresolved, case)
    perturbed = _pair_eligibility(
        supported.model_copy(update={"score": 999.0}),
        unresolved.model_copy(update={"score": -999.0}),
        case,
    )
    assert baseline.s1_eligible == perturbed.s1_eligible
    assert baseline.s2_eligible == perturbed.s2_eligible
    assert baseline.s3_eligible == perturbed.s3_eligible
