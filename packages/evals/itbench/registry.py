"""Model-safe snapshot tool registry for ITBench-Lite."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from packages.contracts import EvidenceSourceType
from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend
from packages.investigation.contracts import ToolRepeatPolicy
from packages.investigation.registry import ReadOnlyToolRegistry, RegisteredTool
from packages.investigation.tool_contracts import ToolArguments
from packages.tools import ToolRequest


@dataclass(frozen=True, slots=True)
class ITBenchTool:
    """One named, bounded operation over the immutable snapshot."""

    name: str
    category: ITBenchEvidenceCategory
    purpose: str
    arguments: dict[str, Any]


class ITBenchSnapshotToolRegistry:
    """Allow-list with no filesystem, shell, SQL, or evaluator access."""

    _TOOLS = (
        ITBenchTool(
            "itbench_alerts",
            ITBenchEvidenceCategory.ALERTS,
            "Read bounded alert observations.",
            {
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        ),
        ITBenchTool(
            "itbench_metrics",
            ITBenchEvidenceCategory.METRICS,
            "Read bounded metric observations.",
            {
                "service": {"type": "string"},
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        ),
        ITBenchTool(
            "itbench_kubernetes_events",
            ITBenchEvidenceCategory.K8S_EVENTS,
            "Read bounded Kubernetes event observations.",
            {
                "namespace": {"type": "string"},
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        ),
        ITBenchTool(
            "itbench_kubernetes_objects",
            ITBenchEvidenceCategory.K8S_OBJECTS,
            "Read bounded Kubernetes object observations.",
            {
                "namespace": {"type": "string"},
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        ),
        ITBenchTool(
            "itbench_logs",
            ITBenchEvidenceCategory.LOGS,
            "Read bounded OpenTelemetry log observations.",
            {
                "service": {"type": "string"},
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        ),
        ITBenchTool(
            "itbench_traces",
            ITBenchEvidenceCategory.TRACES,
            "Read bounded OpenTelemetry trace observations.",
            {
                "service": {"type": "string"},
                "trace_id": {"type": "string"},
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        ),
    )

    def __init__(self, backend: ITBenchSnapshotBackend) -> None:
        self.backend = backend
        self._by_name = {tool.name: tool for tool in self._TOOLS}

    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self._TOOLS)

    def descriptors(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "name": tool.name,
                "version": "1",
                "purpose": tool.purpose,
                "evidence_type": tool.category.value,
                "arguments": {
                    "type": "object",
                    "properties": tool.arguments,
                    "additionalProperties": False,
                },
            }
            for tool in self._TOOLS
        )

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate the fixed contract and query only the selected category."""
        tool = self._by_name.get(name)
        if tool is None:
            raise PermissionError("ITBench tool is not registered")
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")
        allowed = set(tool.arguments)
        unknown = set(arguments) - allowed
        if unknown:
            raise ValueError(f"unknown tool arguments: {sorted(unknown)}")
        for key in arguments:
            if key != "limit" and not isinstance(arguments[key], str):
                raise TypeError(f"{key} must be a string")
        return self.backend.query(tool.category, dict(arguments))

    def public_context(self) -> str:
        """Serialize only investigator-safe context and tool descriptors."""
        alert_context = self.backend.query(
            ITBenchEvidenceCategory.ALERTS, {"limit": min(20, self.backend.max_rows)}
        )
        payload = {
            "scenario": self.backend.scenario.public_context(),
            "alerts": alert_context["records"],
            "tool_catalog": list(self.descriptors()),
            "evidence_context_version": "itbench_lite_snapshot_v1",
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def investigation_registry(self) -> ReadOnlyToolRegistry:
        """Adapt these external tools to the existing read-only runtime."""
        return ReadOnlyToolRegistry(
            tuple(
                RegisteredTool(
                    name=tool.name,
                    version="1",
                    operation=tool.name,
                    source_type=_SOURCE_TYPES[tool.category],
                    tool=_SnapshotRuntimeTool(tool.name, tool.category, self.backend),
                    purpose=tool.purpose,
                    argument_model=_ARGUMENT_MODELS[tool.category],
                    repeat_policy=ToolRepeatPolicy.FIXED_WINDOW,
                )
                for tool in self._TOOLS
            )
        )


class _SnapshotQueryArguments(ToolArguments):
    """Shared bounded filter fields for snapshot-backed runtime tools."""

    pattern: str | None = Field(default=None, min_length=1, max_length=100)
    limit: int = Field(default=50, ge=1, le=50)


class _SnapshotServiceArguments(_SnapshotQueryArguments):
    service: str | None = Field(default=None, min_length=1, max_length=100)


class _SnapshotNamespaceArguments(_SnapshotQueryArguments):
    namespace: str | None = Field(default=None, min_length=1, max_length=100)


class _SnapshotTraceArguments(_SnapshotServiceArguments):
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


class _SnapshotRuntimeTool:
    """Runtime Tool implementation over one fixed observable category."""

    def __init__(
        self, name: str, category: ITBenchEvidenceCategory, backend: ITBenchSnapshotBackend
    ) -> None:
        self.name = name
        self.version = "1"
        self._category = category
        self._backend = backend

    def run(self, request: ToolRequest) -> dict[str, Any]:
        """Execute only the selected typed snapshot query."""
        query_arguments = {
            key: value
            for key, value in request.parameters.items()
            if key not in {"operation", "temporal_mode", "observation_window"}
        }
        result = self._backend.query(self._category, query_arguments)
        result["__temporal_mode"] = request.parameters.get("temporal_mode", "INCIDENT_WINDOW")
        observation_window = request.parameters.get("observation_window")
        if isinstance(observation_window, dict):
            result["__effective_time_window"] = observation_window
        return result


_SOURCE_TYPES = {
    ITBenchEvidenceCategory.ALERTS: EvidenceSourceType.ALERT,
    ITBenchEvidenceCategory.METRICS: EvidenceSourceType.METRIC,
    ITBenchEvidenceCategory.K8S_EVENTS: EvidenceSourceType.KUBERNETES,
    ITBenchEvidenceCategory.K8S_OBJECTS: EvidenceSourceType.KUBERNETES,
    ITBenchEvidenceCategory.LOGS: EvidenceSourceType.LOG,
    ITBenchEvidenceCategory.TRACES: EvidenceSourceType.TRACE,
}

_ARGUMENT_MODELS: dict[ITBenchEvidenceCategory, type[ToolArguments]] = {
    ITBenchEvidenceCategory.ALERTS: _SnapshotQueryArguments,
    ITBenchEvidenceCategory.METRICS: _SnapshotServiceArguments,
    ITBenchEvidenceCategory.K8S_EVENTS: _SnapshotNamespaceArguments,
    ITBenchEvidenceCategory.K8S_OBJECTS: _SnapshotNamespaceArguments,
    ITBenchEvidenceCategory.LOGS: _SnapshotServiceArguments,
    ITBenchEvidenceCategory.TRACES: _SnapshotTraceArguments,
}


__all__ = ["ITBenchSnapshotToolRegistry", "ITBenchTool"]
