"""Persist a real live-tool result and verify evidence ownership rules."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from urllib.request import urlopen
from uuid import UUID, uuid4

from packages.contracts import Evidence, EvidenceSourceType, TimeWindow
from packages.storage import EvidenceWriteRepository, create_database_engine, create_session_factory
from packages.tools import (
    BoundedToolExecutor,
    PrometheusBackend,
    StorageToolAuditSink,
    ToolRequest,
    ToolResponse,
    metrics_tool,
)


def main() -> int:
    control_plane_url = os.getenv("CONTROL_PLANE_URL", "http://localhost:18081")
    with urlopen(f"{control_plane_url}/api/v1/incidents", timeout=5) as response:
        incidents = json.loads(response.read())
    if not incidents:
        raise RuntimeError("live evidence check requires one live incident")
    incident_id = UUID(incidents[-1]["incident_id"])

    database_url = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:15432/agentic_sre"
    )
    session_factory = create_session_factory(create_database_engine(database_url))
    sink = StorageToolAuditSink(session_factory)
    executor = BoundedToolExecutor(sink)
    request = ToolRequest(
        tool_name="metrics",
        tool_version="1",
        incident_id=incident_id,
        timeout_ms=5_000,
        max_results=100,
        max_bytes=1_000_000,
        parameters={"operation": "service_error_rate", "service": "payment-service"},
    )
    result = executor.execute(
        metrics_tool(PrometheusBackend("http://localhost:19090").query), request
    )
    if not isinstance(result, ToolResponse):
        raise RuntimeError(f"live metrics tool failed: {result}")

    now = datetime.now(UTC)
    evidence = Evidence(
        incident_id=incident_id,
        source_type=EvidenceSourceType.METRIC,
        source_system="prometheus",
        observation=result.data,
        time_window=TimeWindow(starts_at=now - timedelta(minutes=5), ends_at=now),
        tool_call_id=request.tool_call_id,
        raw_result_reference=f"prometheus://tool-call/{request.tool_call_id}",
        collected_at=now,
    )
    with session_factory() as session:
        EvidenceWriteRepository(session).append(evidence)
        try:
            EvidenceWriteRepository(session).append(
                evidence.model_copy(update={"incident_id": uuid4()})
            )
        except ValueError:
            pass
        else:
            raise RuntimeError("cross-incident evidence was accepted")
        try:
            EvidenceWriteRepository(session).append(
                evidence.model_copy(update={"tool_call_id": uuid4()})
            )
        except ValueError:
            pass
        else:
            raise RuntimeError("fabricated tool reference was accepted")
    print(f"live evidence provenance: PASS incident={incident_id} tool_call={request.tool_call_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
