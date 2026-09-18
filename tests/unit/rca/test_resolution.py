"""Structural resolution tests independent of benchmark labels or scores."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from packages.rca.demo import demo_source
from packages.rca.engine import diagnose
from packages.rca.information_gap import derive_information_gaps
from packages.rca.model import (
    CausalHop,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    FrontierStatus,
    GapDimension,
    Hypothesis,
    Resolution,
    ResolutionReasonCode,
    StructuralAlternative,
)
from packages.rca.resolution import (
    dominates,
    hypothesis_signature,
    resolution_audit_records,
    resolve_hypotheses,
    structurally_equivalent,
)

_ONSET = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(namespace="shop", kind=kind, name=name)


def _finding(
    entity: EntityRef,
    kind: FindingKind,
    evidence: str,
    *,
    role: EvidenceTemporalRole = EvidenceTemporalRole.INITIATING,
    seconds: int = -60,
    source_class: str = "condition",
) -> Finding:
    return Finding(
        kind=kind,
        entity=entity,
        at=_ONSET + timedelta(seconds=seconds),
        incident_onset=_ONSET,
        onset_delta_seconds=seconds,
        temporal_role=role,
        summary=f"{kind.value} on {entity.name}",
        evidence_ids=(evidence,),
        details={"source_class": source_class},
    )


def _hpa(name: str, *, score: float = 10, extra: Finding | None = None) -> Hypothesis:
    actor = _entity("HorizontalPodAutoscaler", name)
    target = _entity("Deployment", f"{name}-workload")
    initiating = _finding(actor, FindingKind.AUTOSCALING_FAILURE, f"{name}-hpa")
    findings = [initiating]
    initiating_findings = [initiating]
    if extra is not None:
        findings.append(extra)
        if extra.temporal_role is EvidenceTemporalRole.INITIATING:
            initiating_findings.append(extra)
    support = _finding(
        target,
        FindingKind.FAILURE_EVENT,
        f"{name}-failure",
        role=EvidenceTemporalRole.SUPPORTING,
        seconds=60,
        source_class="kubernetes_event",
    )
    findings.append(support)
    return Hypothesis(
        hypothesis_id=f"hypothesis:{name}",
        causal_actor=actor,
        members=(actor, target),
        manifestations=(target,),
        findings=tuple(findings),
        initiating_findings=tuple(initiating_findings),
        supporting_findings=(support,),
        causal_paths=((CausalHop(source=actor, relation="scales", target=target),),),
        linked_symptoms=("latency",),
        causal_explanation="PATH",
        score=score,
    )


def test_equivalent_hpa_hypotheses_are_ambiguous() -> None:
    trace = resolve_hypotheses((_hpa("ad"), _hpa("recommendation")))

    assert trace.state is Resolution.AMBIGUOUS
    assert len(trace.leading_hypothesis_ids) == 2
    assert "causal_actor_identity" in trace.unresolved_dimensions


def test_equal_score_different_evidence_structure_is_not_ambiguous() -> None:
    config_actor = _entity("ConfigMap", "settings")
    workload = _entity("Deployment", "checkout")
    config_finding = _finding(config_actor, FindingKind.CONFIG_CHANGE, "config-change")
    upstream = Hypothesis(
        hypothesis_id="hypothesis:config",
        causal_actor=config_actor,
        members=(config_actor, workload),
        manifestations=(workload,),
        findings=(config_finding,),
        initiating_findings=(config_finding,),
        causal_paths=(
            (
                CausalHop(source=config_actor, relation="configures", target=workload),
                CausalHop(
                    source=workload,
                    relation="serves",
                    target=_entity("Service", "checkout"),
                ),
            ),
        ),
        linked_symptoms=("latency",),
        causal_explanation="PATH",
        score=10,
    )
    downstream_finding = _finding(
        _entity("Pod", "checkout-1"),
        FindingKind.CONTAINER_FAILURE,
        "container-failure",
        role=EvidenceTemporalRole.SUPPORTING,
        seconds=60,
        source_class="kubernetes_event",
    )
    downstream = Hypothesis(
        hypothesis_id="hypothesis:pod",
        causal_actor=downstream_finding.entity,
        members=(downstream_finding.entity,),
        findings=(downstream_finding,),
        supporting_findings=(downstream_finding,),
        linked_symptoms=("latency",),
        causal_explanation="DIRECT",
        score=10,
    )

    trace = resolve_hypotheses((upstream, downstream))

    assert trace.state is Resolution.RESOLVED
    assert trace.leading_hypothesis_ids == (upstream.hypothesis_id,)


def test_small_score_difference_does_not_resolve_equivalent_structure() -> None:
    trace = resolve_hypotheses((_hpa("a", score=10), _hpa("b", score=9.9)))

    assert trace.state is Resolution.AMBIGUOUS


def test_resolution_is_invariant_to_rank_order_and_score() -> None:
    left = _hpa("left", score=100)
    right = _hpa("right", score=1)

    forward = resolve_hypotheses((left, right))
    reverse = resolve_hypotheses((right, left))

    assert forward.state is reverse.state is Resolution.AMBIGUOUS
    assert set(forward.leading_hypothesis_ids) == set(reverse.leading_hypothesis_ids)
    assert not dominates(left, right)
    assert not dominates(right, left)


def test_resolution_trace_contains_structured_discriminator_and_audit() -> None:
    selected = _hpa(
        "selected",
        extra=_finding(
            _entity("ConfigMap", "selected-config"),
            FindingKind.CONFIG_CHANGE,
            "selected-config-change",
        ),
    )
    alternative = _hpa("alternative")

    trace = resolve_hypotheses((selected, alternative))

    assert trace.state is Resolution.RESOLVED
    assert trace.decision_basis == "VALID_DOMINANCE"
    assert trace.considered_hypotheses == tuple(
        sorted((selected.hypothesis_id, alternative.hypothesis_id))
    )
    assert trace.plausible_hypotheses
    assert trace.dominance_relations
    assert trace.discriminators[0].kind == "VALID_DOMINANCE"
    assert {item.hypothesis_id for item in trace.hypothesis_audits} == {
        selected.hypothesis_id,
        alternative.hypothesis_id,
    }


def test_contradiction_is_a_structured_resolution_reason() -> None:
    good = _hpa("good")
    late_finding = _finding(
        _entity("Deployment", "late"),
        FindingKind.IMAGE_CHANGE,
        "late-change",
        role=EvidenceTemporalRole.CONSEQUENCE,
        seconds=7200,
    )
    late = Hypothesis(
        hypothesis_id="hypothesis:late-structured",
        causal_actor=late_finding.entity,
        members=(late_finding.entity,),
        findings=(late_finding,),
        contradictory_findings=(late_finding,),
        causal_explanation="PATH",
    )

    trace = resolve_hypotheses((good, late))

    elimination = next(
        item for item in trace.eliminations if item.hypothesis_id == late.hypothesis_id
    )
    assert elimination.code is ResolutionReasonCode.EXPLICIT_TEMPORAL_CONTRADICTION
    assert elimination.evidence_ids == ("late-change",)


def test_unique_initiating_evidence_dominates_shared_support() -> None:
    h1 = _hpa(
        "a",
        extra=_finding(
            _entity("ConfigMap", "a-config"),
            FindingKind.CONFIG_CHANGE,
            "a-config-change",
        ),
    )
    h2 = _hpa("b")

    trace = resolve_hypotheses((h1, h2))

    assert trace.state is Resolution.RESOLVED
    assert trace.leading_hypothesis_ids == (h1.hypothesis_id,)
    assert trace.distinguishing_facts


def test_late_consequence_does_not_establish_dominance() -> None:
    stronger = _hpa(
        "stronger",
        extra=_finding(
            _entity("Pod", "late-pod"),
            FindingKind.FAILURE_EVENT,
            "late-consequence",
            role=EvidenceTemporalRole.CONSEQUENCE,
            seconds=43 * 60,
            source_class="kubernetes_event",
        ),
    )
    weaker = _hpa("weaker")

    assert not dominates(stronger, weaker)


def test_extra_supporting_evidence_does_not_establish_dominance() -> None:
    stronger = _hpa(
        "stronger",
        extra=_finding(
            _entity("Pod", "supporting-pod"),
            FindingKind.CONTAINER_FAILURE,
            "extra-support",
            role=EvidenceTemporalRole.SUPPORTING,
            seconds=90,
            source_class="kubernetes_event",
        ),
    )
    weaker = _hpa("weaker")

    assert not dominates(stronger, weaker)


def test_additional_aligned_initiating_evidence_can_establish_dominance() -> None:
    stronger = _hpa(
        "stronger",
        extra=_finding(
            _entity("ConfigMap", "incident-config"),
            FindingKind.CONFIG_CHANGE,
            "aligned-config-change",
        ),
    )
    weaker = _hpa("weaker")

    assert dominates(stronger, weaker)


def test_contradicted_alternative_is_eliminated() -> None:
    good = _hpa("good")
    late = _finding(
        _entity("Deployment", "late"),
        FindingKind.IMAGE_CHANGE,
        "late-change",
        role=EvidenceTemporalRole.CONSEQUENCE,
        seconds=7200,
    )
    bad = Hypothesis(
        hypothesis_id="hypothesis:late",
        causal_actor=late.entity,
        members=(late.entity,),
        findings=(late,),
        contradictory_findings=(late,),
        causal_explanation="PATH",
        linked_symptoms=("latency",),
        score=100,
    )

    trace = resolve_hypotheses((good, bad))

    assert trace.state is Resolution.RESOLVED
    assert bad.hypothesis_id in trace.eliminated_hypotheses
    assert "contradictory evidence" in trace.elimination_reasons[0]


def test_no_signal_and_weak_candidate_are_insufficient() -> None:
    assert resolve_hypotheses(()).state is Resolution.INSUFFICIENT_EVIDENCE
    weak = _hpa("weak").model_copy(
        update={
            "initiating_findings": (),
            "findings": tuple(
                finding
                for finding in _hpa("weak").findings
                if finding.temporal_role is EvidenceTemporalRole.SUPPORTING
            ),
        }
    )
    assert resolve_hypotheses((weak,)).state is Resolution.INSUFFICIENT_EVIDENCE


def test_grouped_actor_and_manifestation_is_one_resolvable_episode() -> None:
    hypothesis = _hpa("payments")
    trace = resolve_hypotheses((hypothesis,))

    assert trace.state is Resolution.RESOLVED
    assert len(hypothesis.members) == 2
    assert hypothesis.manifestations == (_entity("Deployment", "payments-workload"),)


def test_signature_is_name_independent_and_score_independent() -> None:
    left = _hpa("left", score=10)
    right = _hpa("right", score=3)

    assert structurally_equivalent(left, right)
    assert hypothesis_signature(left) == hypothesis_signature(right)


def test_rename_and_irrelevant_hypothesis_do_not_change_ambiguity() -> None:
    base = resolve_hypotheses((_hpa("one"), _hpa("two")))
    renamed = resolve_hypotheses((_hpa("alpha"), _hpa("omega")))
    unrelated = Hypothesis(
        hypothesis_id="hypothesis:unrelated",
        causal_actor=_entity("Pod", "unrelated"),
        members=(_entity("Pod", "unrelated"),),
        causal_explanation="UNLINKED",
        score=1000,
    )
    expanded = resolve_hypotheses((_hpa("one"), _hpa("two"), unrelated))

    assert base.state is renamed.state is expanded.state is Resolution.AMBIGUOUS
    assert len(base.leading_hypothesis_ids) == len(renamed.leading_hypothesis_ids)
    assert len(expanded.leading_hypothesis_ids) == 2


def test_duplicate_evidence_does_not_change_resolution() -> None:
    original = _hpa("one")
    duplicate = original.model_copy(
        update={"findings": original.findings + (original.findings[0],)}
    )

    assert resolve_hypotheses((original, _hpa("two"))).state is Resolution.AMBIGUOUS
    assert resolve_hypotheses((duplicate, _hpa("two"))).state is Resolution.AMBIGUOUS


def test_information_gap_describes_hpa_ambiguity_without_executing_tools() -> None:
    left = _hpa("ad")
    right = _hpa("recommendation")
    trace = resolve_hypotheses((left, right))

    gaps = derive_information_gaps((left, right), trace)

    target_gap = next(gap for gap in gaps if gap.dimension.value == "AUTOSCALING_TARGET_STATE")
    assert trace.state is Resolution.AMBIGUOUS
    assert target_gap.resolvability.value == "RESOLVABLE"
    assert "describe" in target_gap.candidate_tools
    assert "events" in target_gap.candidate_tools
    assert any(outcome.kind.value == "NO_DATA" for outcome in target_gap.discriminating_outcomes)
    assert not any(
        outcome.kind.value == "SUPPORTS" and not outcome.hypothesis_ids
        for outcome in target_gap.discriminating_outcomes
    )


def test_structural_alternative_is_queryable_but_not_a_resolver_hypothesis() -> None:
    actor = _entity("ConfigMap", "checkout-config")
    alternative = StructuralAlternative(
        alternative_id="alternative:checkout-config",
        actor=actor,
        role="configuration_source",
        structural_basis=("uses_config:ConfigMap:Deployment",),
        queryable_dimensions=(GapDimension.CONFIG_DIFFERENCE,),
        observation_targets=(actor,),
    )
    hypothesis = _hpa("selected")
    trace = resolve_hypotheses((hypothesis,))
    gaps = derive_information_gaps((hypothesis,), trace, structural_alternatives=(alternative,))

    assert trace.state is Resolution.RESOLVED
    assert hypothesis.causal_actor != actor
    gap = next(item for item in gaps if item.dimension is GapDimension.CONFIG_DIFFERENCE)
    assert gap.hypothesis_ids == (hypothesis.hypothesis_id,)
    assert gap.alternative_ids == (alternative.alternative_id,)
    assert alternative.status is FrontierStatus.UNEXPLORED

    exhausted = alternative.model_copy(update={"status": FrontierStatus.QUERIED_NO_CAUSAL_FINDING})
    assert derive_information_gaps((hypothesis,), trace, structural_alternatives=(exhausted,)) == ()


def test_information_gap_marks_observed_hpa_state_as_already_observed() -> None:
    def observed_hpa(name: str) -> Hypothesis:
        state = _finding(
            _entity("HorizontalPodAutoscaler", name),
            FindingKind.AUTOSCALING_FAILURE,
            f"{name}-state",
        ).model_copy(
            update={
                "details": {
                    "conditions": ["ScalingActive=False"],
                    "targets": [f"Deployment/{name}-workload"],
                }
            }
        )
        return _hpa(name, extra=state)

    left = observed_hpa("ad")
    right = observed_hpa("recommendation")
    gaps = derive_information_gaps((left, right), resolve_hypotheses((left, right)))

    target_gap = next(gap for gap in gaps if gap.dimension.value == "AUTOSCALING_TARGET_STATE")
    assert target_gap.resolvability.value == "ALREADY_OBSERVED"
    assert target_gap.candidate_tools == ()


def test_information_gaps_are_order_and_name_invariant() -> None:
    left = _hpa("one")
    right = _hpa("two")
    forward = derive_information_gaps((left, right), resolve_hypotheses((left, right)))
    reverse = derive_information_gaps((right, left), resolve_hypotheses((right, left)))

    assert [gap.dimension for gap in forward] == [gap.dimension for gap in reverse]
    assert [gap.gap_id for gap in forward] == [gap.gap_id for gap in reverse]


def test_discriminating_evidence_removes_ambiguity_and_gaps() -> None:
    left = _hpa(
        "left",
        extra=_finding(
            _entity("ConfigMap", "left-config"),
            FindingKind.CONFIG_CHANGE,
            "left-config-change",
        ),
    )
    right = _hpa("right")
    trace = resolve_hypotheses((left, right))

    assert trace.state is Resolution.RESOLVED
    assert derive_information_gaps((left, right), trace) == ()


def test_unresolvable_metric_gap_is_not_presented_as_support() -> None:
    left = _hpa(
        "left",
        extra=_finding(
            _entity("Pod", "left-pod"),
            FindingKind.RESOURCE_PRESSURE,
            "left-pressure",
            role=EvidenceTemporalRole.SUPPORTING,
            seconds=60,
            source_class="metric",
        ),
    )
    right = _hpa(
        "right",
        extra=_finding(
            _entity("Pod", "right-pod"),
            FindingKind.RESOURCE_PRESSURE,
            "right-pressure",
            role=EvidenceTemporalRole.SUPPORTING,
            seconds=60,
            source_class="metric",
        ),
    )
    trace = resolve_hypotheses((left, right))
    gaps = derive_information_gaps((left, right), trace)

    pressure_gap = next(gap for gap in gaps if gap.dimension.value == "RESOURCE_PRESSURE")
    assert pressure_gap.resolvability.value == "UNRESOLVABLE_WITH_CURRENT_TOOLS"
    assert all(
        outcome.kind.value != "SUPPORTS" or outcome.hypothesis_ids
        for outcome in pressure_gap.discriminating_outcomes
    )


def test_resolution_audit_explains_structurally_similar_eliminated_pairs() -> None:
    good = _hpa("good")
    weak_base = _hpa("weak-one").model_copy(
        update={
            "causal_explanation": "UNLINKED",
            "causal_paths": (),
            "findings": (),
            "initiating_findings": (),
            "supporting_findings": (),
        }
    )
    weak_other = weak_base.model_copy(
        update={
            "hypothesis_id": "hypothesis:weak-two",
            "causal_actor": _entity("HorizontalPodAutoscaler", "weak-two"),
        }
    )
    trace = resolve_hypotheses((good, weak_base, weak_other))
    diagnosis = diagnose(demo_source()).model_copy(
        update={"resolution": trace.state, "resolution_trace": trace}
    )

    assert trace.state is Resolution.RESOLVED
    records = resolution_audit_records(diagnosis)

    assert len(records) == 1
    assert records[0]["classification"] == "ONLY_ONE_PLAUSIBLE"
    selected = cast(dict[str, object], records[0]["selected"])
    comparison_pair = cast(list[dict[str, object]], records[0]["comparison_pair"])
    assert selected["hypothesis_id"] == good.hypothesis_id
    assert all(not item["plausible"] for item in comparison_pair)


def test_legacy_diagnosis_documents_backfill_resolution_without_losing_root_cause() -> None:
    diagnosis = diagnose(demo_source())
    document = diagnosis.model_dump(mode="json")
    document.pop("resolution")
    document.pop("resolution_trace")
    document.pop("ambiguous_hypotheses")

    restored = type(diagnosis).model_validate(document)

    assert restored.root_cause == diagnosis.root_cause
    assert restored.resolution is Resolution.RESOLVED
