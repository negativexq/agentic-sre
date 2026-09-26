"""Persistence adapters and repositories."""

from packages.rca.model import JournalEntry
from packages.storage.database import create_database_engine, create_session_factory, session_scope

# Imported for its side effect: every ORM Session refuses to mutate evidence rows.
from packages.storage.evidence_guard import AuthoritativeEvidenceMutation
from packages.storage.repositories import (
    AlertRepository,
    ChangeRecordRepository,
    DiagnosisRepository,
    EmailDeliveryRepository,
    EventRepository,
    EvidenceRepository,
    EvidenceRequirementRepository,
    IncidentEventRepository,
    IncidentNotFoundError,
    IncidentRepository,
    InvestigationRunRepository,
    LogObservationRepository,
    ObjectVersionRepository,
    ReportRepository,
)

__all__ = [
    "AlertRepository",
    "AuthoritativeEvidenceMutation",
    "ChangeRecordRepository",
    "DiagnosisRepository",
    "EvidenceRequirementRepository",
    "EmailDeliveryRepository",
    "EventRepository",
    "EvidenceRepository",
    "IncidentEventRepository",
    "IncidentNotFoundError",
    "IncidentRepository",
    "InvestigationRunRepository",
    "LogObservationRepository",
    "JournalEntry",
    "ObjectVersionRepository",
    "ReportRepository",
    "create_database_engine",
    "create_session_factory",
    "session_scope",
]
