"""Tool audit persistence test."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.storage.database import create_session_factory
from packages.storage.models import Base, ToolCallRow
from packages.tools import BoundedToolExecutor, StorageToolAuditSink, ToolRequest, metrics_tool

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def test_tool_execution_can_persist_audit_record(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'tool-audit.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    request = ToolRequest(
        tool_name="metrics",
        tool_version="1",
        incident_id=uuid4(),
        tool_call_id=uuid4(),
        timeout_ms=100,
        max_results=10,
        max_bytes=1000,
        parameters={"operation": "service_latency"},
    )
    result = BoundedToolExecutor(StorageToolAuditSink(factory)).execute(
        metrics_tool(lambda _operation, _parameters: {"records": [{"p95": 0.2}]}), request
    )

    with Session(engine) as session:
        row = session.scalar(
            select(ToolCallRow).where(ToolCallRow.tool_call_id == request.tool_call_id)
        )
        assert row is not None
        assert row.incident_id == request.incident_id
        assert row.tool_name == "metrics"
    assert result.tool_call_id == request.tool_call_id
    engine.dispose()
