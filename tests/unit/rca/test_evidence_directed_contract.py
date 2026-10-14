"""Benchmark-independent acquisition invariants; no dataset or fault labels."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from packages.rca.engine import build_case, diagnose_case
from packages.rca.investigation.candidates import CandidateDiscriminator, ObservationCandidate
from packages.rca.investigation.evidence_targets import (
    candidate_evidence_targets,
    evidence_target_specificity,
)
from packages.rca.investigation.selection import score_observation_candidate
from packages.rca.model import (
    EntityRef,
    GapDimension,
    GapOutcome,
    GapOutcomeKind,
    InvestigationQuery,
)
from packages.rca.source import InMemorySource


def candidate(
    *, comparisons: tuple[str, ...] = ("rival",), capability: str = "runtime_traces"
) -> ObservationCandidate:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return ObservationCandidate(
        candidate_id="read",
        capability=capability,
        target=EntityRef(namespace="ns", kind="Deployment", name="caller"),
        query=InvestigationQuery(start=start, end=start + timedelta(minutes=5)),
        gap_ids=("gap",),
        dimensions=(GapDimension.DEPENDENCY_HEALTH,),
        hypothesis_ids=("claim",),
        alternative_ids=(),
        discriminators=(
            CandidateDiscriminator(
                gap_id="gap",
                dimension=GapDimension.DEPENDENCY_HEALTH,
                missing_fact="observed caller/peer operation",
                support_outcomes=(
                    GapOutcome(kind=GapOutcomeKind.SUPPORTS, hypothesis_ids=("claim",)),
                ),
                comparison_hypothesis_ids=comparisons,
                comparison_alternative_ids=(),
                no_data_outcomes=(),
            ),
        ),
    )


def test_more_comparison_ids_cannot_increase_target_specificity() -> None:
    original = candidate()
    expanded = candidate(comparisons=tuple(f"unrelated-{i}" for i in range(100)))
    diagnosis = diagnose_case(build_case(InMemorySource(name="contracts")))
    assert (
        score_observation_candidate(
            candidate=original, diagnosis=diagnosis
        ).utility.discrimination_value
        == score_observation_candidate(
            candidate=expanded, diagnosis=diagnosis
        ).utility.discrimination_value
    )
    assert evidence_target_specificity(original) == evidence_target_specificity(expanded)


def test_duplicate_discriminator_cannot_inflate_specificity() -> None:
    original = candidate()
    duplicate = replace(original, discriminators=original.discriminators * 20)
    assert evidence_target_specificity(original) == evidence_target_specificity(duplicate)


def test_relation_target_names_concrete_decoder_inputs_without_authority() -> None:
    targets = candidate_evidence_targets(candidate())
    assert targets
    assert targets[0].kind == "RELATION"
    assert (
        "direct CLIENT/SERVER parent relationship in one trace" in targets[0].required_observations
    )
    assert targets[0].consumer_ids
    assert not targets[0].resolution_authority
    assert not targets[0].no_data_is_exclusion


def test_unknown_capability_is_explicitly_unmapped() -> None:
    unknown = replace(candidate(), capability="unknown_capability")
    targets = candidate_evidence_targets(unknown)
    assert targets[0].kind == "UNMAPPED"
    assert evidence_target_specificity(unknown) == 0


def test_discovery_is_valuable_without_a_transition_certificate() -> None:
    from packages.rca.model import InvestigationDiscriminatorKind

    original = candidate(capability="incident_events")
    discriminator = replace(
        original.discriminators[0],
        kind=InvestigationDiscriminatorKind.DISCOVERY,
        dimension=GapDimension.EVENT_SEQUENCE,
        support_outcomes=(),
        unknown_slots=("initiating_object", "event_sequence"),
        expected_fact_families=("incident_event",),
        possible_outcomes=("NEW_ACTOR", "NO_DATA"),
    )
    discovery = replace(
        original, dimensions=(GapDimension.EVENT_SEQUENCE,), discriminators=(discriminator,)
    )
    diagnosis = diagnose_case(build_case(InMemorySource(name="discovery")))
    scored = score_observation_candidate(candidate=discovery, diagnosis=diagnosis)
    assert scored.transition_certificates == ()
    assert scored.utility.discrimination_value == 2
    assert scored.evidence_targets[0].kind == "DISCOVERY"
    assert scored.evidence_targets[0].required_observations == discriminator.unknown_slots


def test_leader_rival_swap_preserves_value_and_contracted_tie_choice() -> None:
    from packages.rca.investigation.selection import active_choice_dominates_baseline
    from packages.rca.model import Hypothesis, Resolution, ResolutionTrace

    original = candidate(capability="history")
    original = replace(
        original,
        dimensions=(GapDimension.CHANGE_TIMING,),
        discriminators=(replace(original.discriminators[0], dimension=GapDimension.CHANGE_TIMING),),
    )
    hypothesis = Hypothesis(hypothesis_id="claim", causal_actor=original.target)
    diagnosis = diagnose_case(build_case(InMemorySource(name="symmetry"))).model_copy(
        update={
            "hypothesis": hypothesis,
            "resolution_trace": ResolutionTrace(
                state=Resolution.AMBIGUOUS,
                leading_hypothesis_ids=("claim",),
                unresolved_hypotheses=("claim", "rival"),
            ),
        }
    )
    assert diagnosis.resolution_trace is not None
    rival_diagnosis = diagnosis.model_copy(
        update={
            "resolution_trace": diagnosis.resolution_trace.model_copy(
                update={"leading_hypothesis_ids": ("rival",)}
            )
        }
    )
    leader = score_observation_candidate(candidate=original, diagnosis=diagnosis)
    rival = score_observation_candidate(candidate=original, diagnosis=rival_diagnosis)
    assert (
        leader.utility.expected_elimination_value == rival.utility.expected_elimination_value == 3
    )
    assert leader.utility.expected_decision_impact == rival.utility.expected_decision_impact
    assert leader.transition_certificates == rival.transition_certificates
    assert active_choice_dominates_baseline(rival, leader) == (
        True,
        "equivalent_contracted_observation_stable_choice",
    )
    certificate = leader.transition_certificates[0]
    assert any(
        target.required_observations == certificate.preconditions
        and certificate.transition_rule_id in target.consumer_ids
        for target in leader.evidence_targets
    )


def test_duplicate_metadata_does_not_inflate_frontier_coverage() -> None:
    original = candidate()
    duplicate = replace(original, discriminators=original.discriminators * 20)
    diagnosis = diagnose_case(build_case(InMemorySource(name="dedup")))
    a = score_observation_candidate(candidate=original, diagnosis=diagnosis)
    b = score_observation_candidate(candidate=duplicate, diagnosis=diagnosis)
    assert a.utility.frontier_coverage == b.utility.frontier_coverage
    assert a.utility.discrimination_value == b.utility.discrimination_value


def test_cached_trace_risk_uses_only_visible_scoped_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.rca.investigation.candidates import _visible_known_refs
    from packages.rca.model import TraceSpanObservation

    original = candidate()
    assert original.query.start is not None
    visible = TraceSpanObservation(
        trace_id="t",
        span_id="s",
        service="caller",
        start_at=original.query.start,
        evidence_id="trace:visible",
        semantic_attributes={"k8s.namespace.name": "ns", "k8s.deployment.name": "caller"},
    )
    foreign = visible.model_copy(
        update={
            "trace_id": "foreign",
            "evidence_id": "trace:foreign",
            "semantic_attributes": {"k8s.namespace.name": "other", "k8s.deployment.name": "caller"},
        }
    )
    outside = visible.model_copy(
        update={
            "trace_id": "old",
            "evidence_id": "trace:old",
            "start_at": original.query.start - timedelta(hours=1),
        }
    )
    source = InMemorySource(name="cache", trace_items=[visible, foreign, outside])
    case = build_case(source)

    def forbidden_read() -> None:
        raise AssertionError("planning must not read telemetry")

    monkeypatch.setattr(source, "trace_observations", forbidden_read)
    assert _visible_known_refs(
        case=case, capability="runtime_traces", target=original.target, query=original.query
    ) == {"trace:visible"}
    request_query = original.query.model_copy(update={"source_log_ids": ("log:request",)})
    assert (
        _visible_known_refs(
            case=case, capability="runtime_traces", target=original.target, query=request_query
        )
        == set()
    )
    bound = replace(original, query=request_query)
    assert (
        candidate_evidence_targets(bound)[0].reacquisition_reason
        == "REQUEST_RELATION_RECONSTRUCTION"
    )


def test_recorded_unknown_rule_deficit_is_preserved_without_an_invented_route() -> None:
    from packages.rca.investigation.evidence_targets import recorded_evidence_deficits
    from packages.rca.model import (
        HypothesisResolutionAudit,
        HypothesisSignature,
        Resolution,
        ResolutionTrace,
        RootSupportRecord,
        RootSupportStatus,
    )

    diagnosis = diagnose_case(build_case(InMemorySource(name="deficit"))).model_copy(
        update={
            "resolution_trace": ResolutionTrace(
                state=Resolution.AMBIGUOUS,
                hypothesis_audits=(
                    HypothesisResolutionAudit(
                        hypothesis_id="claim",
                        signature=HypothesisSignature(),
                        plausible=True,
                        root_support=(
                            RootSupportRecord(
                                rule_id="unregistered.rule",
                                rule_version="1",
                                support_kind="execution",
                                status=RootSupportStatus.NOT_FIRED,
                                reasons=("EXACT_REQUIREMENT_MISSING",),
                                decisive_evidence_ids=("evidence:observed",),
                            ),
                        ),
                    ),
                ),
            )
        }
    )
    (deficit,) = recorded_evidence_deficits(diagnosis, (candidate(),))
    assert deficit.missing_precondition == "EXACT_REQUIREMENT_MISSING"
    assert deficit.evidence_ids == ("evidence:observed",)
    assert deficit.routing == "NO_REGISTERED_TOOL_ROUTE"
    assert deficit.candidate_ids == ()


def test_planning_projection_cannot_change_plain_rca_digest() -> None:
    from packages.rca.epistemic_digest import diagnosis_epistemic_digest
    from packages.rca.investigation.evidence_targets import recorded_evidence_deficits

    case = build_case(InMemorySource(name="plain"))
    diagnosis = diagnose_case(case)
    before = diagnosis_epistemic_digest(diagnosis)
    score_observation_candidate(candidate=candidate(), diagnosis=diagnosis)
    recorded_evidence_deficits(diagnosis, (candidate(),))
    assert diagnosis_epistemic_digest(diagnose_case(case)) == before
