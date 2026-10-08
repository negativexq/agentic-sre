"""An execution that began after the onset never initiates it (m21 contract §19)."""

from __future__ import annotations

from datetime import timedelta

from rca_builders import alert, at, ref
from test_fault_execution_support import OFF, applied, failure, recovered, source, spawned

from packages.rca.engine import build_case, diagnose_case
from packages.rca.model import EvidenceTemporalRole, Finding, FindingKind
from packages.rca.ranking import annotate_temporal_roles
from packages.rca.signals import extract_symptoms

ONSET = at(6)
GRACE = timedelta(minutes=15)


def role(kind: FindingKind, minutes: float, onset: object = ONSET) -> EvidenceTemporalRole:
    finding = Finding(kind=kind, entity=ref("chaos/NetworkChaos/x"), at=at(minutes), summary="s")
    (annotated,) = annotate_temporal_roles([finding], onset, GRACE)  # type: ignore[arg-type]
    return annotated.temporal_role


def test_an_execution_recorded_after_the_onset_is_after_onset_not_initiating() -> None:
    assert role(FindingKind.FAULT_INJECTION, 6 + 5 / 60) is EvidenceTemporalRole.AFTER_ONSET
    assert role(FindingKind.FAULT_SCHEDULE, 18) is EvidenceTemporalRole.AFTER_ONSET


def test_within_the_recording_precision_it_still_initiates() -> None:
    assert role(FindingKind.FAULT_INJECTION, 6 + 1 / 60) is EvidenceTemporalRole.INITIATING


def test_age_is_never_a_reason_an_old_execution_still_initiates() -> None:
    assert role(FindingKind.FAULT_INJECTION, -120) is EvidenceTemporalRole.INITIATING


def test_a_change_observed_late_keeps_the_grace() -> None:
    assert role(FindingKind.SPEC_CHANGE, 11) is EvidenceTemporalRole.INITIATING


def test_an_unknown_onset_leaves_roles_as_they_were() -> None:
    assert role(FindingKind.FAULT_INJECTION, 30, onset=None) is EvidenceTemporalRole.AMBIGUOUS


def test_alerts_before_coverage_are_recorded_as_uncertainty_not_as_the_onset() -> None:
    symptoms = extract_symptoms(
        [alert("Latency", "checkout", 2), alert("Errors", "checkout", 6)],
        alert_observation_start=at(4),
    )
    assert symptoms.onset == at(6)  # the causal onset is unchanged
    assert symptoms.began_before_coverage and symptoms.earliest_alert_start == at(2)
    covered = extract_symptoms([alert("Errors", "checkout", 6)], alert_observation_start=at(4))
    assert not covered.began_before_coverage and covered.earliest_alert_start == at(6)


def test_a_spawn_applied_after_the_onset_is_not_supported_as_its_initiator() -> None:
    # the alert begins at minute 6; the schedule's experiment is applied at minute 7
    late = [spawned(7), applied(7), recovered(9), failure(8)]
    case = build_case(source(late))
    diagnosis = diagnose_case(case, config=OFF)
    for hypothesis in case.hypotheses:
        if hypothesis.causal_actor.kind in {"Schedule", "NetworkChaos"}:
            assert not any(
                f.temporal_role is EvidenceTemporalRole.INITIATING for f in hypothesis.findings
            )
    assert diagnosis.leading_actor_tier != "STRONG"
