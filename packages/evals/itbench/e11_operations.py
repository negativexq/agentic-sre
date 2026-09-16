"""Deterministic availability policy for E11 semantic operations."""

from __future__ import annotations

from typing import Any, cast

from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.e11_observability import ObservedEntity, ObservedEntityCatalog
from packages.evals.itbench.snapshot_backend import (
    _record_matches_entity,
    classify_structured_log,
)


def comparable_peer_count(catalog: ObservedEntityCatalog, entity: ObservedEntity) -> int:
    """Count peers with an explicit shared owner/workload relationship."""
    owner_ids = {
        target if source == entity.canonical else source
        for source, target, relation in entity.relationships
        if relation == "owner"
    }
    if not owner_ids:
        return 0
    return sum(
        1
        for peer in catalog.entities()
        if peer.canonical != entity.canonical
        and peer.namespace == entity.namespace
        and any(
            (source == peer.canonical and target in owner_ids)
            or (target == peer.canonical and source in owner_ids)
            for source, target, relation in peer.relationships
            if relation == "owner"
        )
    )


def available_e11_operations(
    entity: ObservedEntity,
    *,
    comparable_peers: int = 0,
    backend: Any | None = None,
    incident_available: bool | None = None,
) -> tuple[str, ...]:
    """Expose only operations supported by observed evidence for one candidate."""
    operations = ["ENTITY_CONTEXT"]
    sources = entity.source_categories | entity.provenance
    backend_capabilities = _backend_capabilities(backend, entity)
    if "k8s_events" in sources or "DIRECT_K8S_EVENT" in sources:
        operations.append("EVENT_ANALYSIS")
    if "logs" in sources or "LOG_RESOURCE" in sources or backend_capabilities.get("logs"):
        operations.append("LOG_ANALYSIS")
    if "metrics" in sources or "METRIC_RESOURCE" in sources or backend_capabilities.get("metrics"):
        operations.append("METRIC_ANOMALIES")
    if "traces" in sources or "TRACE_RESOURCE" in sources or backend_capabilities.get("traces"):
        operations.append("TRACE_ERROR_TREE")
    if "k8s_objects" in sources or "DIRECT_K8S_OBJECT" in sources:
        operations.append("SPEC_ANALYSIS")
    if comparable_peers >= 1:
        operations.append("COMPARE_REPLICAS")
    if (incident_available is not False) and (entity.first_observed or entity.last_observed):
        operations.append("VERIFY_TEMPORAL_ALIGNMENT")
    # Recent change is intentionally absent unless a trusted change source is
    # added; current snapshots do not provide defensible history by default.
    return tuple(dict.fromkeys(operations))


def _backend_capabilities(backend: Any | None, entity: ObservedEntity) -> dict[str, bool]:
    if backend is None:
        return {}
    cache = getattr(backend, "_e11_operation_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        backend._e11_operation_cache = cache
    if entity.canonical in cache:
        return cast(dict[str, bool], cache[entity.canonical])
    value = {
        "logs": _backend_has_logs(backend, entity, cache),
        "metrics": _backend_has_metrics(backend, entity, cache),
        "traces": _backend_has_traces(backend, entity, cache),
    }
    cache[entity.canonical] = value
    return value


def _capability_records(
    backend: Any, cache: dict[str, Any], category: ITBenchEvidenceCategory
) -> tuple[dict[str, Any], ...]:
    """Cache bounded source records once for capability checks.

    Availability checks run once per visible candidate per turn.  Calling the
    full semantic scanners from that path repeatedly re-reads large snapshot
    files and made the offline canary needlessly expensive.  The actual
    operation still performs its own bounded, typed query when selected.
    """
    key = f"records:{category.value}"
    records = cache.get(key)
    if isinstance(records, tuple):
        return records
    getter = getattr(backend, "records", None)
    if callable(getter):
        records = tuple(getter(category))
    else:
        records = tuple(backend.complete_source_records(category))
    cache[key] = records
    return records


def _backend_has_logs(backend: Any | None, entity: ObservedEntity, cache: dict[str, Any]) -> bool:
    if backend is None:
        return False
    return any(
        _record_matches_entity(item.get("record"), entity.canonical)
        and classify_structured_log(item.get("record", {}))[0] == "ERROR"
        for item in _capability_records(backend, cache, ITBenchEvidenceCategory.LOGS)
        if isinstance(item.get("record"), dict)
    )


def _backend_has_metrics(
    backend: Any | None, entity: ObservedEntity, cache: dict[str, Any]
) -> bool:
    if backend is None:
        return False
    return any(
        _record_matches_entity(item.get("record"), entity.canonical)
        and isinstance(item.get("record"), dict)
        and any(key in item["record"] for key in ("Value", "value", "metric_value"))
        for item in _capability_records(backend, cache, ITBenchEvidenceCategory.METRICS)
    )


def _backend_has_traces(backend: Any | None, entity: ObservedEntity, cache: dict[str, Any]) -> bool:
    if backend is None:
        return False
    return any(
        _record_matches_entity(item.get("record"), entity.canonical)
        for item in _capability_records(backend, cache, ITBenchEvidenceCategory.TRACES)
    )


__all__ = ["available_e11_operations", "comparable_peer_count"]
