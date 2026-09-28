"""M21 F1: influence-channel coverage records for initiated changes (audit only, §4)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from rca_builders import at, config_change_source

from packages.rca.channels import _closure_outcome, channel_assessments
from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.model import (
    ChannelApplicability,
    ChannelEvaluation,
    ChannelState,
    EntityRef,
    RootSupportStatus,
    TraceSpanObservation,
    TraceSpanStatus,
)

A = ChannelApplicability.APPLICABLE
NA = ChannelApplicability.NOT_APPLICABLE


def _assessments(spans: list[TraceSpanObservation] | None = None):  # type: ignore[no-untyped-def]
    case = build_case(config_change_source())
    config = EngineConfig()
    return case, channel_assessments(
        case.hypotheses,
        topology=case.topology,
        history=case.source.object_history(),
        symptom_entities=set(case.context.symptom_entities),
        symptom_services=case.symptoms.services,
        runtime_graph=case.runtime_graph,
        trace_spans=spans if spans is not None else list(case.source.trace_observations()),
        onset=case.symptoms.onset,
        grace=config.ranking.verification_onset_grace,
    )


def _evaluation(channel: str, state: ChannelState | None) -> ChannelEvaluation:
    return ChannelEvaluation(
        channel=channel,
        applicability=A if state is not None else NA,
        applicability_basis="test",
        state=state,
    )


@pytest.mark.parametrize(
    ("states", "outcome"),
    [
        ((None, None), RootSupportStatus.INAPPLICABLE),  # I11: no applicable channel
        ((ChannelState.NO_PATH_COVERED, ChannelState.PATH), RootSupportStatus.NOT_FIRED),
        ((ChannelState.NO_PATH_COVERED, ChannelState.UNCOVERED), RootSupportStatus.INAPPLICABLE),
        ((ChannelState.NO_PATH_COVERED, ChannelState.UNKNOWN), RootSupportStatus.INAPPLICABLE),
        ((ChannelState.NO_PATH_COVERED, None), RootSupportStatus.FIRED),
    ],
)
def test_the_section_4_3_table(
    states: tuple[ChannelState | None, ...], outcome: RootSupportStatus
) -> None:
    evaluations = [_evaluation(f"X{i}", state) for i, state in enumerate(states)]
    assert _closure_outcome(evaluations, True, at(0), at(20))[0] is outcome


def test_an_incomplete_closure_or_an_unknown_window_is_inapplicable() -> None:
    covered = [_evaluation("K", ChannelState.NO_PATH_COVERED)]
    assert _closure_outcome(covered, False, at(0), at(20)) == (
        RootSupportStatus.INAPPLICABLE,
        "CLOSURE_INCOMPLETE",
    )
    assert _closure_outcome(covered, True, at(0), None) == (
        RootSupportStatus.INAPPLICABLE,
        "WINDOW_UNKNOWN",
    )


def test_an_applicable_channel_needs_a_state_and_an_inapplicable_one_has_none() -> None:
    with pytest.raises(ValueError):
        ChannelEvaluation(channel="K", applicability=A, applicability_basis="x")
    with pytest.raises(ValueError):
        ChannelEvaluation(
            channel="K", applicability=NA, applicability_basis="x", state=ChannelState.PATH
        )


def test_only_initiated_changes_are_assessed_and_k_is_never_covered_in_v1() -> None:
    case, assessments = _assessments()
    initiated = {h.hypothesis_id for h in case.hypotheses if h.initiating_findings}
    assert set(assessments) == initiated and initiated
    for assessment in assessments.values():
        k = next(e for e in assessment.evaluations if e.channel == "K")
        assert k.state in {ChannelState.PATH, ChannelState.UNCOVERED}
        assert assessment.closure_outcome is not RootSupportStatus.FIRED


def test_applicability_does_not_depend_on_observed_evidence() -> None:
    _, without = _assessments(spans=[])
    span = TraceSpanObservation(
        trace_id="0" * 31 + "1",
        span_id="0" * 15 + "1",
        service="checkout",
        span_kind="SERVER",
        start_at=at(11),
        end_at=at(11) + timedelta(seconds=1),
        status=TraceSpanStatus.ERROR,
        semantic_attributes={"k8s.pod.name": "checkout-5d8f7c9b4-abcde"},
        evidence_id="tempo:x",
    )
    _, with_spans = _assessments(spans=[span])
    for hypothesis_id, assessment in without.items():
        before = {
            e.channel: (e.applicability, e.applicability_basis) for e in assessment.evaluations
        }
        after = {
            e.channel: (e.applicability, e.applicability_basis)
            for e in with_spans[hypothesis_id].evaluations
        }
        assert before == after


def test_a_namespace_closure_makes_the_control_plane_channel_applicable_and_uncovered() -> None:
    case, _ = _assessments()
    namespace = EntityRef.parse("_cluster/Namespace/shop")
    hypothesis = next(h for h in case.hypotheses if h.initiating_findings)
    changed = hypothesis.model_copy(update={"causal_actor": namespace})
    config = EngineConfig()
    (assessment,) = channel_assessments(
        [changed],
        topology=case.topology,
        history=case.source.object_history(),
        symptom_entities=set(case.context.symptom_entities),
        symptom_services=case.symptoms.services,
        runtime_graph=case.runtime_graph,
        trace_spans=[],
        onset=case.symptoms.onset,
        grace=config.ranking.verification_onset_grace,
    ).values()
    control = next(e for e in assessment.evaluations if e.channel == "C")
    assert (control.applicability, control.state) == (A, ChannelState.UNCOVERED)
    assert assessment.closure_outcome is RootSupportStatus.INAPPLICABLE


def test_the_record_is_attached_to_audits_and_outside_the_digest() -> None:
    diagnosis = diagnose_case(build_case(config_change_source()))
    trace = diagnosis.resolution_trace
    assert trace is not None
    assert any(audit.channel_assessment is not None for audit in trace.hypothesis_audits)
    stripped = diagnosis.model_copy(
        update={
            "resolution_trace": trace.model_copy(
                update={
                    "hypothesis_audits": tuple(
                        audit.model_copy(update={"channel_assessment": None})
                        for audit in trace.hypothesis_audits
                    )
                }
            )
        }
    )
    assert diagnosis_epistemic_digest(stripped) == diagnosis_epistemic_digest(diagnosis)
