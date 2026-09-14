"""Model-safe snapshot tool registry for ITBench-Lite."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend


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
        payload = {
            "scenario": self.backend.scenario.public_context(),
            "alerts": list(self.backend.records(ITBenchEvidenceCategory.ALERTS)[:20]),
            "tool_catalog": list(self.descriptors()),
            "evidence_context_version": "itbench_lite_snapshot_v1",
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = ["ITBenchSnapshotToolRegistry", "ITBenchTool"]
