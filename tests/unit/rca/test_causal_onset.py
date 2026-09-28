"""M21 amendment 4 P3: the causal onset needs alert-channel coverage; unknown fails closed."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from rca_builders import alert, at, config_change_source

from packages.rca.engine import build_case, diagnose_case
from packages.rca.model import Confidence, EvidenceTemporalRole
from packages.rca.signals import (
    ONSET_ANCHORED,
    ONSET_UNKNOWN_NO_ALERT_COVERAGE,
    ONSET_UNKNOWN_NO_NEW_EPISODE,
    extract_symptoms,
)

ALERTS = [alert("Watchdog", "prometheus", 0), alert("RequestErrorRate", "checkout", 12)]


def _diagnosis(coverage: datetime | None):  # type: ignore[no-untyped-def]
    case = build_case(replace(config_change_source(), alert_coverage_start=coverage))
    return case, diagnose_case(case)


def test_covered_alert_channel_anchors_the_onset_and_verifies() -> None:
    symptoms = extract_symptoms(ALERTS, alert_observation_start=at(0))
    assert (symptoms.onset, symptoms.onset_basis) == (at(12), ONSET_ANCHORED)
    _, diagnosis = _diagnosis(at(0))
    assert diagnosis.confidence is Confidence.VERIFIED


def test_without_w_the_causal_onset_is_unknown() -> None:
    symptoms = extract_symptoms(ALERTS, alert_observation_start=None)
    assert symptoms.onset is None
    assert symptoms.onset_basis == ONSET_UNKNOWN_NO_ALERT_COVERAGE
    # The reference time stays available for query windows only.
    assert symptoms.reference_time == at(12)
    case, _ = _diagnosis(None)
    assert case.symptoms.onset is None


def test_episodes_that_began_before_w_give_no_initiating_authority() -> None:
    symptoms = extract_symptoms(ALERTS, alert_observation_start=at(13))
    assert symptoms.onset is None
    assert symptoms.onset_basis == ONSET_UNKNOWN_NO_NEW_EPISODE
    case, diagnosis = _diagnosis(at(13))
    findings = [finding for hypothesis in case.hypotheses for finding in hypothesis.findings]
    assert findings
    assert all(finding.temporal_role is not EvidenceTemporalRole.INITIATING for finding in findings)
    assert diagnosis.resolution_trace.plausible_hypotheses == ()


def test_an_unknown_onset_validates_no_config_mention() -> None:
    # The config change names the failing service; before P3 an unknown onset let
    # ranking's temporally_valid() accept it, and the diagnosis became VERIFIED.
    _, diagnosis = _diagnosis(None)
    assert diagnosis.confidence is not Confidence.VERIFIED
    assert all(
        predicate.status.value != "PASS"
        for trace in [diagnosis.verification]
        if trace is not None
        for predicate in trace.predicates
        if predicate.name == "initiating_evidence_near_onset"
    )
