"""Contract integrity tests for the deterministic foundation vocabulary."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.contracts import (
    ActionExecution,
    ActionExecutionStatus,
    ActionRequest,
    ActionType,
    Alert,
    AlertSource,
    AlertStatus,
    AuthorizedAction,
    Evidence,
    EvidenceSourceType,
    Hypothesis,
    HypothesisStatus,
    Incident,
    IncidentEvent,
    IncidentEventType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
    PolicyDecision,
    PolicyEvaluation,
    RemediationProposal,
    RiskClass,
    TimeWindow,
    VerificationCheck,
    VerificationResult,
    VerificationStatus,
)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
INCIDENT_ID = uuid4()
TOOL_CALL_ID = uuid4()
EVIDENCE_ID = uuid4()


def test_incident_and_alert_round_trip_through_json() -> None:
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="Payment errors elevated",
        created_at=NOW,
        updated_at=NOW,
    )
    alert = Alert(
        alert_name="HighErrorRate",
        service="payment-service",
        namespace="sre-demo",
        cluster="agentic-sre",
        starts_at=NOW,
        fingerprint="fp-123",
        status=AlertStatus.FIRING,
        source=AlertSource.PROMETHEUS,
        labels={"severity": "critical"},
    )

    assert Incident.model_validate_json(incident.model_dump_json()) == incident
    assert Alert.model_validate_json(alert.model_dump_json()) == alert


def test_incident_is_immutable() -> None:
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.WARNING,
        source=IncidentSource.SYSTEM,
        title="Immutable incident",
        created_at=NOW,
        updated_at=NOW,
    )

    with pytest.raises(ValidationError):
        incident.status = IncidentStatus.TRIAGING


def test_event_is_immutable_and_json_round_trip_safe() -> None:
    event = IncidentEvent(
        incident_id=INCIDENT_ID,
        event_type=IncidentEventType.INCIDENT_CREATED,
        timestamp=NOW,
        correlation_id=uuid4(),
        payload={"source": "alertmanager"},
    )

    assert IncidentEvent.model_validate_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError):
        event.event_type = IncidentEventType.STATE_CHANGED


def test_evidence_and_hypothesis_preserve_provenance_references() -> None:
    evidence = Evidence(
        incident_id=INCIDENT_ID,
        source_type=EvidenceSourceType.METRIC,
        source_system="prometheus",
        observation={"active_connections": 98},
        time_window=TimeWindow(starts_at=NOW, ends_at=NOW),
        tool_call_id=TOOL_CALL_ID,
        raw_result_reference="prometheus://query/abc",
        collected_at=NOW,
    )
    hypothesis = Hypothesis(
        incident_id=INCIDENT_ID,
        affected_component="payment-service",
        mechanism="database connection pressure",
        suspected_trigger="pool configuration change",
        evidence_ids=[evidence.evidence_id],
        status=HypothesisStatus.PROPOSED,
    )

    assert Evidence.model_validate_json(evidence.model_dump_json()) == evidence
    assert hypothesis.evidence_ids == [evidence.evidence_id]


def test_remediation_policy_action_and_verification_contracts_round_trip() -> None:
    proposal = RemediationProposal(
        incident_id=INCIDENT_ID,
        action_type=ActionType.RESTART_DEPLOYMENT,
        target="sre-demo/payment-service",
        reason="Restore service health after validated fault removal.",
        evidence_ids=[EVIDENCE_ID],
        risk_class=RiskClass.HIGH,
        reversible=True,
    )
    policy = PolicyEvaluation(
        decision=PolicyDecision.DENY,
        reason="Writes are disabled in v0.1.0.",
        evaluated_at=NOW,
        policy_version="v0.1.0-foundation",
    )
    request = ActionRequest(
        incident_id=INCIDENT_ID,
        action_type=proposal.action_type,
        target=proposal.target,
        reason=proposal.reason,
        evidence_ids=proposal.evidence_ids,
    )
    authorization = AuthorizedAction(
        request_id=request.request_id,
        decision=policy.decision,
        authorized_at=NOW,
        authorized_by="foundation-policy",
    )
    execution = ActionExecution(
        authorization_id=authorization.authorization_id,
        status=ActionExecutionStatus.NOT_STARTED,
    )
    result = VerificationResult(
        incident_id=INCIDENT_ID,
        status=VerificationStatus.RECOVERED,
        checks=[
            VerificationCheck(
                name="error-rate-below-threshold",
                description="Error rate is below the service SLO threshold.",
                passed=True,
            )
        ],
        observed_at=NOW,
        summary="Service recovered.",
    )

    for model in (proposal, policy, request, authorization, execution, result):
        assert type(model).model_validate_json(model.model_dump_json()) == model


def test_invalid_enum_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Incident(
            status="UNKNOWN",  # type: ignore[arg-type]
            severity=IncidentSeverity.WARNING,
            source=IncidentSource.SYSTEM,
            title="Invalid status",
            created_at=NOW,
            updated_at=NOW,
        )


def test_required_fields_and_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Incident(  # type: ignore[call-arg]
            status=IncidentStatus.OPEN,
            severity=IncidentSeverity.WARNING,
            source=IncidentSource.SYSTEM,
            created_at=NOW,
            updated_at=NOW,
        )

    with pytest.raises(ValidationError):
        Incident(
            status=IncidentStatus.OPEN,
            severity=IncidentSeverity.WARNING,
            source=IncidentSource.SYSTEM,
            title="Unknown field",
            created_at=NOW,
            updated_at=NOW,
            unexpected="reject-me",  # type: ignore[call-arg]
        )


def test_invalid_time_window_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ends_at"):
        TimeWindow(starts_at=NOW, ends_at=NOW.replace(hour=11))
