"""Deterministic incident smoke scenarios."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from packages.contracts import (
    AlertmanagerAlertPayload,
    Evidence,
    EvidenceSourceType,
    TimeWindow,
)
from packages.evidence import EvidenceService
from packages.incident import IncidentManager, normalize_alert
from packages.storage import IncidentEventRepository
from packages.tools import (
    BoundedToolExecutor,
    InMemoryToolAuditSink,
    ToolRequest,
    ToolResponse,
    kubernetes_read_tool,
    logs_tool,
    metrics_tool,
    traces_tool,
)


@dataclass(frozen=True, slots=True)
class SmokeResult:
    """Evidence that one deterministic smoke scenario completed."""

    scenario: str
    incident_id: UUID
    evidence_count: int
    event_types: tuple[str, ...]


class DeterministicSmokeHarness:
    """Run alert→incident→read-only evidence→resolution scenarios."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def run(self, scenario: str, alert_name: str, signal: str) -> SmokeResult:
        """Execute one known fault class without root-cause inference."""
        now = datetime.now(UTC)
        payload = AlertmanagerAlertPayload(
            status="firing",
            labels={
                "alertname": alert_name,
                "service": "payment-service",
                "namespace": "sre-demo",
                "cluster": "agentic-sre",
                "severity": "critical",
            },
            annotations={"description": signal},
            startsAt=now,
        )
        with self._session_factory() as session:
            incident = IncidentManager(session).ingest(normalize_alert(payload), now=now)
            audit = InMemoryToolAuditSink()
            executor = BoundedToolExecutor(audit)
            evidence_service = EvidenceService()
            tools = (
                (metrics_tool, "metrics", "service_error_rate", EvidenceSourceType.METRIC),
                (logs_tool, "logs", "query_logs", EvidenceSourceType.LOG),
                (traces_tool, "traces", "search_traces", EvidenceSourceType.TRACE),
                (
                    kubernetes_read_tool,
                    "kubernetes",
                    "get_resource_state",
                    EvidenceSourceType.KUBERNETES,
                ),
            )
            for tool_factory, name, operation, source_type in tools:
                tool = tool_factory(
                    lambda op, _params: {"records": [{"signal": signal, "operation": op}]}
                )
                request = ToolRequest(
                    tool_name=name,
                    tool_version="1",
                    incident_id=incident.incident_id,
                    tool_call_id=uuid4(),
                    timeout_ms=100,
                    max_results=10,
                    max_bytes=10_000,
                    parameters={"operation": operation},
                )
                result = executor.execute(tool, request)
                if isinstance(result, ToolResponse):
                    evidence_service.register_tool_call(request.tool_call_id, incident.incident_id)
                    evidence_service.add(
                        Evidence(
                            incident_id=incident.incident_id,
                            source_type=source_type,
                            source_system=name,
                            observation=result.data,
                            time_window=TimeWindow(starts_at=now, ends_at=now),
                            tool_call_id=request.tool_call_id,
                            raw_result_reference=f"{name}://{request.tool_call_id}",
                            collected_at=now,
                        )
                    )
            resolved = payload.model_copy(update={"status": "resolved", "ends_at": now})
            IncidentManager(session).ingest(normalize_alert(resolved), now=now)
            events = IncidentEventRepository(session).list_for_incident(incident.incident_id)
            return SmokeResult(
                scenario=scenario,
                incident_id=incident.incident_id,
                evidence_count=len(evidence_service.list_for_incident(incident.incident_id)),
                event_types=tuple(event.event_type.value for event in events),
            )
