"""Bounded executor and audit sinks for read-only tools."""

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from packages.storage import ToolCallRepository
from packages.tools.contracts import (
    ToolErrorCode,
    ToolFailure,
    ToolRequest,
    ToolResponse,
    ToolResult,
)


class Tool(Protocol):
    """Typed read-only tool implementation."""

    name: str
    version: str

    def run(self, request: ToolRequest) -> dict[str, Any]:
        """Execute one bounded operation."""


@dataclass(frozen=True, slots=True)
class ToolAuditRecord:
    """One invocation record retained by the audit sink."""

    request: ToolRequest
    result: ToolResult
    started_at: datetime
    finished_at: datetime


class ToolAuditSink(Protocol):
    """Audit persistence boundary."""

    def record(self, record: ToolAuditRecord) -> None:
        """Persist an invocation."""


class InMemoryToolAuditSink:
    """Deterministic audit sink for tests."""

    def __init__(self) -> None:
        self.records: list[ToolAuditRecord] = []

    def record(self, record: ToolAuditRecord) -> None:
        self.records.append(record)


class StorageToolAuditSink:
    """Persist tool-call records through the storage repository boundary."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def record(self, record: ToolAuditRecord) -> None:
        with self._session_factory() as session:
            ToolCallRepository(session).append(
                tool_call_id=record.request.tool_call_id,
                incident_id=record.request.incident_id,
                tool_name=record.request.tool_name,
                tool_version=record.request.tool_version,
                request=record.request.model_dump(mode="json"),
                response=record.result.model_dump(mode="json"),
                started_at=record.started_at,
                finished_at=record.finished_at,
            )


class BoundedToolExecutor:
    """Enforce timeout, response size, and typed failures for every tool call."""

    def __init__(self, audit_sink: ToolAuditSink | None = None) -> None:
        self._audit_sink = audit_sink

    def execute(self, tool: Tool, request: ToolRequest) -> ToolResult:
        """Run a tool with caller-provided bounds and record the outcome."""
        started_at = datetime.now().astimezone()
        executor: ThreadPoolExecutor | None = None
        try:
            if request.tool_name != tool.name or request.tool_version != tool.version:
                raise ValueError("tool identity does not match request")
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(tool.run, request)
            data = future.result(timeout=request.timeout_ms / 1000)
            if not isinstance(data, dict):
                raise ValueError("tool backend returned a non-object result")
            encoded = json.dumps(data, default=str, separators=(",", ":")).encode("utf-8")
            result_count = (
                len(data.get("records", [])) if isinstance(data.get("records", []), list) else 1
            )
            if result_count > request.max_results or len(encoded) > request.max_bytes:
                result: ToolResult = ToolFailure(
                    tool_call_id=request.tool_call_id,
                    code=ToolErrorCode.RESULT_LIMIT_EXCEEDED,
                    message="tool result exceeded configured bounds",
                )
            else:
                result = ToolResponse(
                    tool_call_id=request.tool_call_id,
                    data=data,
                    result_count=result_count,
                )
        except TimeoutError:
            result = ToolFailure(
                tool_call_id=request.tool_call_id,
                code=ToolErrorCode.TOOL_TIMEOUT,
                message="tool execution exceeded timeout",
            )
        except PermissionError as error:
            result = ToolFailure(
                tool_call_id=request.tool_call_id,
                code=ToolErrorCode.PERMISSION_DENIED,
                message=str(error),
            )
        except LookupError as error:
            result = ToolFailure(
                tool_call_id=request.tool_call_id,
                code=ToolErrorCode.NOT_FOUND,
                message=str(error),
            )
        except ConnectionError as error:
            result = ToolFailure(
                tool_call_id=request.tool_call_id,
                code=ToolErrorCode.BACKEND_UNAVAILABLE,
                message=str(error),
            )
        except ValueError as error:
            result = ToolFailure(
                tool_call_id=request.tool_call_id,
                code=ToolErrorCode.INVALID_QUERY,
                message=str(error),
            )
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        finished_at = datetime.now().astimezone()
        if self._audit_sink is not None:
            self._audit_sink.record(ToolAuditRecord(request, result, started_at, finished_at))
        return result
