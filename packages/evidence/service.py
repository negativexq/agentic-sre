"""Evidence service enforcing tool-call provenance."""

from uuid import UUID

from packages.contracts import Evidence


class CrossIncidentEvidenceError(ValueError):
    """Raised when evidence references a tool call from another incident."""


class EvidenceService:
    """Deterministic evidence registry for the foundation release."""

    def __init__(self) -> None:
        self._tool_call_incidents: dict[UUID, UUID] = {}
        self._evidence: list[Evidence] = []

    def register_tool_call(self, tool_call_id: UUID, incident_id: UUID) -> None:
        """Register the incident ownership of one audited tool call."""
        self._tool_call_incidents[tool_call_id] = incident_id

    def add(self, evidence: Evidence) -> Evidence:
        """Persist evidence only when its tool call belongs to the incident."""
        owner = self._tool_call_incidents.get(evidence.tool_call_id)
        if owner is None or owner != evidence.incident_id:
            raise CrossIncidentEvidenceError("tool call does not belong to evidence incident")
        self._evidence.append(evidence)
        return evidence

    def list_for_incident(self, incident_id: UUID) -> list[Evidence]:
        """Return evidence for one incident."""
        return [item for item in self._evidence if item.incident_id == incident_id]
