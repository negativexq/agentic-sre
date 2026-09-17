"""Structural resolution tests independent of benchmark labels or scores."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.demo import demo_source
from packages.rca.engine import diagnose
from packages.rca.model import (
    CausalHop,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
    Resolution,
)
from packages.rca.resolution import (
    hypothesis_signature,
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


def test_legacy_diagnosis_documents_backfill_resolution_without_losing_root_cause() -> None:
    diagnosis = diagnose(demo_source())
    document = diagnosis.model_dump(mode="json")
    document.pop("resolution")
    document.pop("resolution_trace")
    document.pop("ambiguous_hypotheses")

    restored = type(diagnosis).model_validate(document)

    assert restored.root_cause == diagnosis.root_cause
    assert restored.resolution is Resolution.RESOLVED
