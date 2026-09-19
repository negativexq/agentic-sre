"""Provider-neutral, deterministic distributed-trace observation parsing."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from typing import Any

from packages.rca.model import TraceSpanStatus

_STATUS_KEYS = ("status.code", "StatusCode", "status_code", "Status", "status")
_RESOURCE_KEYS = ("ResourceAttributes", "resource_attributes", "resource", "Resource")
_SPAN_KEYS = ("SpanAttributes", "span_attributes", "attributes", "Attributes")
_ERROR_KEYS = ("error.type", "exception.type", "exception.message")
SEMANTIC_ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "service.name",
        "service.namespace",
        "service.instance.id",
        "k8s.namespace.name",
        "k8s.pod.name",
        "k8s.deployment.name",
        "k8s.statefulset.name",
        "k8s.daemonset.name",
        "k8s.container.name",
        "peer.service",
        "server.address",
        "server.port",
        "network.peer.address",
        "network.peer.port",
        "rpc.system",
        "rpc.system.name",
        "rpc.service",
        "rpc.method",
        "rpc.response.status_code",
        "rpc.grpc.status_code",
        "http.request.method",
        "http.response.status_code",
        "http.status_code",
        "http.route",
        "db.system",
        "db.namespace",
        "db.operation.name",
        "messaging.system",
        "messaging.destination.name",
        "error.type",
        "exception.type",
    }
)


def parse_trace_mapping(value: Any) -> dict[str, Any]:
    """Parse one structured trace attribute value without executable parsing."""
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
        except (ValueError, SyntaxError, json.JSONDecodeError):
            continue
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _mapping_from_precedence(record: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    for key in keys:
        if key in record:
            parsed = parse_trace_mapping(record[key])
            if parsed:
                return parsed
    return {}


def _non_empty(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _status_value(raw: Any) -> str:
    if isinstance(raw, Mapping):
        raw = raw.get("code", raw.get("value"))
    return "" if raw is None else str(raw).strip().casefold()


def normalize_trace_status(record: Mapping[str, Any]) -> tuple[TraceSpanStatus, str]:
    """Normalize status once for both typed ingestion and backend compatibility."""
    raw: Any = None
    for key in _STATUS_KEYS:
        if key in record:
            raw = record[key]
            break
    resource = _mapping_from_precedence(record, _RESOURCE_KEYS)
    span = _mapping_from_precedence(record, _SPAN_KEYS)
    if raw is None and "status.code" in resource:
        raw = resource["status.code"]
    value = _status_value(raw)
    if value in {"", "unset", "none", "unknown", "0"}:
        status = TraceSpanStatus.UNSET
        reason = "OTel status code 0 is unset" if value == "0" else "trace status is unset"
    elif value in {"ok", "success", "successful", "1"}:
        return TraceSpanStatus.OK, "trace status explicitly indicates success"
    elif value in {"error", "failed", "failure", "2"}:
        return TraceSpanStatus.ERROR, "trace status explicitly indicates an error"
    else:
        status = TraceSpanStatus.UNKNOWN
        reason = "unrecognized trace status representation"
    if status not in {TraceSpanStatus.OK, TraceSpanStatus.ERROR}:
        for key in _ERROR_KEYS:
            if (
                _non_empty(record.get(key))
                or _non_empty(span.get(key))
                or _non_empty(resource.get(key))
            ):
                return TraceSpanStatus.ERROR, f"explicit {key} attribute"
    return status, reason


def semantic_attributes(record: Mapping[str, Any]) -> dict[str, str]:
    """Extract only the bounded provider-neutral semantic attribute allowlist."""
    span = _mapping_from_precedence(record, _SPAN_KEYS)
    resource = _mapping_from_precedence(record, _RESOURCE_KEYS)
    result: dict[str, str] = {}
    for key in sorted(SEMANTIC_ATTRIBUTE_ALLOWLIST):
        if key in record and _non_empty(record[key]):
            value = record[key]
        elif key in span and _non_empty(span[key]):
            value = span[key]
        elif key in resource and _non_empty(resource[key]):
            value = resource[key]
        else:
            continue
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            continue
        result[key] = str(value)
    return result


__all__ = [
    "SEMANTIC_ATTRIBUTE_ALLOWLIST",
    "normalize_trace_status",
    "parse_trace_mapping",
    "semantic_attributes",
]
