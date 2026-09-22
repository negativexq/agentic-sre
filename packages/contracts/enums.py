"""Enumerations shared by deterministic SRE domain contracts."""

from enum import StrEnum


class IncidentStatus(StrEnum):
    """Lifecycle states available to an incident."""

    OPEN = "OPEN"
    TRIAGING = "TRIAGING"
    INVESTIGATING = "INVESTIGATING"
    HYPOTHESIS_FORMED = "HYPOTHESIS_FORMED"
    VALIDATING = "VALIDATING"
    REMEDIATION_PROPOSED = "REMEDIATION_PROPOSED"
    POLICY_EVALUATION = "POLICY_EVALUATION"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


class IncidentSeverity(StrEnum):
    """Operational impact classification for an incident."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class IncidentSource(StrEnum):
    """System that originated an incident."""

    ALERTMANAGER = "ALERTMANAGER"
    CONTROL_PLANE = "CONTROL_PLANE"
    SYSTEM = "SYSTEM"


class AlertStatus(StrEnum):
    """Alert lifecycle status received from an alert source."""

    FIRING = "FIRING"
    RESOLVED = "RESOLVED"


class AlertSource(StrEnum):
    """System that emitted an alert."""

    PROMETHEUS = "PROMETHEUS"
    ALERTMANAGER = "ALERTMANAGER"
    SYSTEM = "SYSTEM"


class IncidentEventType(StrEnum):
    """Immutable events in an incident timeline."""

    INCIDENT_CREATED = "INCIDENT_CREATED"
    ALERT_ATTACHED = "ALERT_ATTACHED"
    ALERT_RESOLVED = "ALERT_RESOLVED"
    ALERT_UPDATED = "ALERT_UPDATED"
    STATE_CHANGED = "STATE_CHANGED"
    EVIDENCE_ADDED = "EVIDENCE_ADDED"
    HYPOTHESIS_CREATED = "HYPOTHESIS_CREATED"
    DIAGNOSIS_STARTED = "DIAGNOSIS_STARTED"
    EVIDENCE_GATHERED = "EVIDENCE_GATHERED"
    DIAGNOSIS_COMPLETED = "DIAGNOSIS_COMPLETED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    ACTION_EXECUTED = "ACTION_EXECUTED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
    INCIDENT_ESCALATED = "INCIDENT_ESCALATED"


class EvidenceSourceType(StrEnum):
    """Kind of operational signal represented by evidence."""

    METRIC = "METRIC"
    LOG = "LOG"
    TRACE = "TRACE"
    KUBERNETES = "KUBERNETES"
    CHANGE = "CHANGE"
    ALERT = "ALERT"


class HypothesisStatus(StrEnum):
    """Status of a recorded hypothesis."""

    PROPOSED = "PROPOSED"
    VALIDATING = "VALIDATING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class RiskClass(StrEnum):
    """Risk classification for a proposed action."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class PolicyDecision(StrEnum):
    """Fail-closed policy outcomes."""

    ALLOW = "ALLOW"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    DENY = "DENY"


class ActionType(StrEnum):
    """Initial infrastructure action vocabulary."""

    SCALE_DEPLOYMENT = "SCALE_DEPLOYMENT"
    RESTART_DEPLOYMENT = "RESTART_DEPLOYMENT"
    ROLLBACK_DEPLOYMENT = "ROLLBACK_DEPLOYMENT"


class ActionExecutionStatus(StrEnum):
    """Execution lifecycle reserved for a later release."""

    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class VerificationStatus(StrEnum):
    """Outcome of post-action or post-fault verification."""

    RECOVERED = "RECOVERED"
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"
    NOT_RECOVERED = "NOT_RECOVERED"
    INCONCLUSIVE = "INCONCLUSIVE"
