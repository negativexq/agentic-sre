"""Deterministic availability policy for E11 semantic operations."""

from __future__ import annotations

from packages.evals.itbench.e11_observability import ObservedEntity, ObservedEntityCatalog


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
    entity: ObservedEntity, *, comparable_peers: int = 0
) -> tuple[str, ...]:
    """Expose only operations supported by observed evidence for one candidate."""
    operations = ["ENTITY_CONTEXT"]
    sources = entity.source_categories | entity.provenance
    if "k8s_events" in sources or "DIRECT_K8S_EVENT" in sources:
        operations.append("EVENT_ANALYSIS")
    if "logs" in sources or "LOG_RESOURCE" in sources:
        operations.append("LOG_ANALYSIS")
    if "metrics" in sources or "METRIC_RESOURCE" in sources:
        operations.append("METRIC_ANOMALIES")
    if "traces" in sources or "TRACE_RESOURCE" in sources:
        operations.append("TRACE_ERROR_TREE")
    if "k8s_objects" in sources or "DIRECT_K8S_OBJECT" in sources:
        operations.append("SPEC_ANALYSIS")
    if comparable_peers >= 1:
        operations.append("COMPARE_REPLICAS")
    if entity.first_observed or entity.last_observed:
        operations.append("VERIFY_TEMPORAL_ALIGNMENT")
    # Recent change is intentionally absent unless a trusted change source is
    # added; current snapshots do not provide defensible history by default.
    return tuple(dict.fromkeys(operations))


__all__ = ["available_e11_operations", "comparable_peer_count"]
