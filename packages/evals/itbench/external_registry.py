"""Named, bounded ITBench semantic tools over immutable snapshot data."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from packages.contracts import EvidenceSourceType
from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.external_context import normalize_alerts
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend
from packages.investigation.contracts import ToolRepeatPolicy
from packages.investigation.registry import ReadOnlyToolRegistry, RegisteredTool
from packages.investigation.tool_contracts import ToolArguments
from packages.tools import ToolRequest


class ExternalQueryArguments(ToolArguments):
    """Common bounded string filters; no query language is accepted."""

    pattern: str | None = Field(default=None, min_length=1, max_length=100)
    limit: int = Field(default=20, ge=1, le=50)


class ExternalEntityArguments(ExternalQueryArguments):
    namespace: str | None = Field(default=None, min_length=1, max_length=255)
    kind: str | None = Field(default=None, min_length=1, max_length=128)
    entity: str | None = Field(default=None, min_length=3, max_length=512)


class ExternalTraceArguments(ExternalQueryArguments):
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


class ExternalMetricArguments(ExternalQueryArguments):
    """Typed metric filters supported by the snapshot columns."""

    service: str | None = Field(default=None, min_length=1, max_length=255)
    namespace: str | None = Field(default=None, min_length=1, max_length=255)


class ITBenchExternalToolRegistry:
    """External allow-list, independent of the internal A1 tool ontology."""

    _SPECS = (
        (
            "itbench_alert_summary",
            "Normalized observable alert signatures.",
            ITBenchEvidenceCategory.ALERTS,
            ExternalQueryArguments,
            10_000,
        ),
        (
            "itbench_entity_search",
            "Search observable Kubernetes entity identities.",
            ITBenchEvidenceCategory.K8S_OBJECTS,
            ExternalEntityArguments,
            5_000,
        ),
        (
            "itbench_entity_context",
            "Read bounded context for one observable entity.",
            ITBenchEvidenceCategory.K8S_OBJECTS,
            ExternalEntityArguments,
            10_000,
        ),
        (
            "itbench_topology",
            "Read bounded observable Kubernetes relationships.",
            ITBenchEvidenceCategory.K8S_OBJECTS,
            ExternalQueryArguments,
            5_000,
        ),
        (
            "itbench_metric_analysis",
            "Read bounded metric observations and generic aggregates.",
            ITBenchEvidenceCategory.METRICS,
            ExternalMetricArguments,
            20_000,
        ),
        (
            "itbench_logs",
            "Read bounded OpenTelemetry logs.",
            ITBenchEvidenceCategory.LOGS,
            ExternalQueryArguments,
            20_000,
        ),
        (
            "itbench_trace_search",
            "Search bounded OpenTelemetry trace records.",
            ITBenchEvidenceCategory.TRACES,
            ExternalTraceArguments,
            20_000,
        ),
        (
            "itbench_trace_detail",
            "Read bounded records for a supplied trace identifier.",
            ITBenchEvidenceCategory.TRACES,
            ExternalTraceArguments,
            20_000,
        ),
        (
            "itbench_kubernetes_events",
            "Read bounded Kubernetes events.",
            ITBenchEvidenceCategory.K8S_EVENTS,
            ExternalQueryArguments,
            10_000,
        ),
        (
            "itbench_kubernetes_objects",
            "Read bounded Kubernetes objects.",
            ITBenchEvidenceCategory.K8S_OBJECTS,
            ExternalQueryArguments,
            10_000,
        ),
    )

    def __init__(self, backend: ITBenchSnapshotBackend) -> None:
        self.backend = backend

    def names(self) -> tuple[str, ...]:
        return tuple(item[0] for item in self._SPECS)

    def descriptors(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "name": name,
                "version": "1",
                "purpose": purpose,
                "evidence_type": category.value,
                "arguments": _descriptor(argument_model),
            }
            for name, purpose, category, argument_model, _timeout in self._SPECS
        )

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = next((item for item in self._SPECS if item[0] == name), None)
        if spec is None:
            raise PermissionError("external tool is not registered")
        _name, _purpose, category, argument_model, _timeout = spec
        parsed = argument_model.model_validate(arguments)
        args = parsed.model_dump(mode="json", exclude_none=True)
        if name == "itbench_alert_summary":
            records = list(normalize_alerts(self.backend))
            return _bounded_records(name, records, args.get("limit", 20), self.backend.max_bytes)
        if name == "itbench_entity_search":
            records = list(self.backend.observable_entities())
            records = [item for item in records if _entity_matches(item, args)]
            return _bounded_records(name, records, args.get("limit", 20), self.backend.max_bytes)
        if name == "itbench_entity_context":
            entity = args.get("entity")
            if not isinstance(entity, str):
                raise ValueError("entity is required")
            return {
                "tool": name,
                **self.backend.query_entity_context(entity, args.get("limit", 20)),
            }
        if name == "itbench_topology":
            return _bounded_records(
                name,
                list(self.backend.topology(limit=args.get("limit", 20))),
                args.get("limit", 20),
                self.backend.max_bytes,
            )
        if name == "itbench_metric_analysis":
            return self.backend.metric_analysis(args)
        if name == "itbench_trace_detail" and isinstance(args.get("trace_id"), str):
            return self.backend.query(
                ITBenchEvidenceCategory.TRACES,
                {"trace_id": args["trace_id"], "limit": args.get("limit", 20)},
            )
        return self.backend.query(category, args)

    def investigation_registry(self) -> ReadOnlyToolRegistry:
        tools = []
        for name, purpose, category, argument_model, timeout_ms in self._SPECS:
            tools.append(
                RegisteredTool(
                    name=name,
                    version="1",
                    operation=name,
                    source_type=EvidenceSourceType.ALERT
                    if name == "itbench_alert_summary"
                    else _SOURCE_TYPES[category],
                    tool=_ExternalRuntimeTool(name, self),
                    purpose=purpose,
                    argument_model=argument_model,
                    repeat_policy=ToolRepeatPolicy.FIXED_WINDOW,
                    timeout_ms=timeout_ms,
                )
            )
        return ReadOnlyToolRegistry(tuple(tools))


class _ExternalRuntimeTool:
    def __init__(self, name: str, registry: ITBenchExternalToolRegistry) -> None:
        self.name = name
        self.version = "1"
        self._registry = registry

    def run(self, request: ToolRequest) -> dict[str, Any]:
        args = {
            key: value
            for key, value in request.parameters.items()
            if key not in {"operation", "temporal_mode", "observation_window"}
        }
        result = self._registry.invoke(self.name, args)
        result["__temporal_mode"] = request.parameters.get("temporal_mode", "INCIDENT_WINDOW")
        if isinstance(request.parameters.get("observation_window"), dict):
            result["__effective_time_window"] = request.parameters["observation_window"]
        return result


def _descriptor(model: type[ToolArguments]) -> dict[str, Any]:
    schema = model.model_json_schema()
    properties = schema.get("properties", {})
    return properties if isinstance(properties, dict) else {}


def _entity_matches(item: dict[str, str], args: dict[str, Any]) -> bool:
    requested = args.get("entity")
    if isinstance(requested, str):
        try:
            if (
                requested.casefold()
                != f"{item['namespace']}/{item['kind']}/{item['name']}".casefold()
            ):
                return False
        except KeyError:
            return False
    for key in ("namespace", "kind"):
        value = args.get(key)
        if isinstance(value, str) and item[key].casefold() != value.casefold():
            return False
    pattern = args.get("pattern")
    return not isinstance(pattern, str) or pattern.casefold() in json.dumps(item).casefold()


def _bounded_records(
    name: str, records: list[dict[str, Any]], limit: int, max_bytes: int
) -> dict[str, Any]:
    selected = records[:limit]
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":"), default=str)
    while len(encoded.encode()) > max_bytes and selected:
        selected.pop()
        encoded = json.dumps(selected, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "tool": name,
        "records": selected,
        "returned_count": len(selected),
        "matching_count_lower_bound": len(selected),
        "truncated": len(records) > len(selected),
    }


_SOURCE_TYPES = {
    ITBenchEvidenceCategory.ALERTS: EvidenceSourceType.ALERT,
    ITBenchEvidenceCategory.METRICS: EvidenceSourceType.METRIC,
    ITBenchEvidenceCategory.K8S_EVENTS: EvidenceSourceType.KUBERNETES,
    ITBenchEvidenceCategory.K8S_OBJECTS: EvidenceSourceType.KUBERNETES,
    ITBenchEvidenceCategory.LOGS: EvidenceSourceType.LOG,
    ITBenchEvidenceCategory.TRACES: EvidenceSourceType.TRACE,
}

__all__ = ["ITBenchExternalToolRegistry"]
