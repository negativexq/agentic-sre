"""Deterministic runtime service dependencies derived from trace parentage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.rca.model import TraceSpanObservation


class RuntimeSpanKind(StrEnum):
    """Provider-neutral span kind used only for runtime edge qualification."""

    UNSPECIFIED = "UNSPECIFIED"
    INTERNAL = "INTERNAL"
    SERVER = "SERVER"
    CLIENT = "CLIENT"
    PRODUCER = "PRODUCER"
    CONSUMER = "CONSUMER"
    UNKNOWN = "UNKNOWN"


class RuntimeEdgeEvidence(StrEnum):
    """Evidence class for one observed cross-service parent relationship."""

    PAIRED_CLIENT_SERVER = "PAIRED_CLIENT_SERVER"
    PAIRED_PRODUCER_CONSUMER = "PAIRED_PRODUCER_CONSUMER"
    CROSS_SERVICE_PARENT = "CROSS_SERVICE_PARENT"


@dataclass(frozen=True)
class CanonicalTraceIndex:
    """The one canonical span view shared by runtime evidence consumers."""

    spans: Mapping[tuple[str, str], TraceSpanObservation]
    conflicting_keys: frozenset[tuple[str, str]]
    input_spans: int
    duplicate_equivalent_rows: int
    conflicting_span_keys: int


def normalize_runtime_span_kind(value: str | None) -> RuntimeSpanKind:
    """Normalize only the provider's explicit span-kind representation."""
    text = value.strip().upper() if value is not None else ""
    mappings = {
        "": RuntimeSpanKind.UNSPECIFIED,
        "0": RuntimeSpanKind.UNSPECIFIED,
        "UNSPECIFIED": RuntimeSpanKind.UNSPECIFIED,
        "SPAN_KIND_UNSPECIFIED": RuntimeSpanKind.UNSPECIFIED,
        "1": RuntimeSpanKind.INTERNAL,
        "INTERNAL": RuntimeSpanKind.INTERNAL,
        "SPAN_KIND_INTERNAL": RuntimeSpanKind.INTERNAL,
        "2": RuntimeSpanKind.SERVER,
        "SERVER": RuntimeSpanKind.SERVER,
        "SPAN_KIND_SERVER": RuntimeSpanKind.SERVER,
        "3": RuntimeSpanKind.CLIENT,
        "CLIENT": RuntimeSpanKind.CLIENT,
        "SPAN_KIND_CLIENT": RuntimeSpanKind.CLIENT,
        "4": RuntimeSpanKind.PRODUCER,
        "PRODUCER": RuntimeSpanKind.PRODUCER,
        "SPAN_KIND_PRODUCER": RuntimeSpanKind.PRODUCER,
        "5": RuntimeSpanKind.CONSUMER,
        "CONSUMER": RuntimeSpanKind.CONSUMER,
        "SPAN_KIND_CONSUMER": RuntimeSpanKind.CONSUMER,
    }
    return mappings.get(text, RuntimeSpanKind.UNKNOWN)


def classify_runtime_pair(
    parent: TraceSpanObservation,
    child: TraceSpanObservation,
) -> RuntimeEdgeEvidence | None:
    """Classify one resolved parent/child relationship without causal meaning."""
    parent_service = parent.service.strip()
    child_service = child.service.strip()
    if parent_service == child_service:
        return None
    parent_kind = normalize_runtime_span_kind(parent.span_kind)
    child_kind = normalize_runtime_span_kind(child.span_kind)
    if parent_kind is RuntimeSpanKind.CLIENT and child_kind is RuntimeSpanKind.SERVER:
        return RuntimeEdgeEvidence.PAIRED_CLIENT_SERVER
    if parent_kind is RuntimeSpanKind.PRODUCER and child_kind is RuntimeSpanKind.CONSUMER:
        return RuntimeEdgeEvidence.PAIRED_PRODUCER_CONSUMER
    return RuntimeEdgeEvidence.CROSS_SERVICE_PARENT


def canonicalize_trace_spans(
    spans: Sequence[TraceSpanObservation],
) -> CanonicalTraceIndex:
    """Apply the P2G-2 duplicate/conflict semantics exactly once."""
    canonical: dict[tuple[str, str], TraceSpanObservation] = {}
    fingerprints: dict[tuple[str, str], set[tuple[str | None, str, RuntimeSpanKind]]] = {}
    conflicting: set[tuple[str, str]] = set()
    duplicate_equivalent_rows = 0
    conflicting_span_keys = 0

    for span in spans:
        key = (span.trace_id, span.span_id)
        fingerprint = (
            span.parent_span_id,
            span.service.strip(),
            normalize_runtime_span_kind(span.span_kind),
        )
        known = fingerprints.setdefault(key, set())
        if fingerprint in known:
            duplicate_equivalent_rows += 1
        else:
            known.add(fingerprint)
            if len(known) >= 2 and key not in conflicting:
                conflicting.add(key)
                conflicting_span_keys += 1
                canonical.pop(key, None)
        if key in conflicting:
            continue
        previous = canonical.get(key)
        if previous is None or span.evidence_id < previous.evidence_id:
            canonical[key] = span

    return CanonicalTraceIndex(
        spans=canonical,
        conflicting_keys=frozenset(conflicting),
        input_spans=len(spans),
        duplicate_equivalent_rows=duplicate_equivalent_rows,
        conflicting_span_keys=conflicting_span_keys,
    )


class RuntimeServiceEdge(BaseModel):
    """One aggregated observed dependency between two runtime services."""

    model_config = ConfigDict(frozen=True)

    caller_service: str = Field(min_length=1)
    callee_service: str = Field(min_length=1)
    first_seen: datetime
    last_seen: datetime
    client_server_pairs: int = Field(ge=0)
    producer_consumer_pairs: int = Field(ge=0)
    cross_service_parent_pairs: int = Field(ge=0)
    evidence_ids: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_edge(self) -> RuntimeServiceEdge:
        if self.caller_service == self.callee_service:
            raise ValueError("runtime service edges must be cross-service")
        if (
            self.client_server_pairs
            + self.producer_consumer_pairs
            + self.cross_service_parent_pairs
            <= 0
        ):
            raise ValueError("runtime service edge needs observed pair evidence")
        if self.first_seen > self.last_seen:
            raise ValueError("runtime service edge has inverted observation range")
        return self

    @property
    def has_strict_evidence(self) -> bool:
        return self.client_server_pairs > 0 or self.producer_consumer_pairs > 0

    @property
    def fallback_only(self) -> bool:
        return not self.has_strict_evidence and self.cross_service_parent_pairs > 0


class RuntimeGraphStats(BaseModel):
    """Deterministic construction accounting for one runtime graph."""

    model_config = ConfigDict(frozen=True)

    input_spans: int = Field(ge=0)
    canonical_spans: int = Field(ge=0)
    duplicate_equivalent_rows: int = Field(ge=0)
    conflicting_span_keys: int = Field(ge=0)
    root_spans: int = Field(ge=0)
    child_spans: int = Field(ge=0)
    resolved_parent_links: int = Field(ge=0)
    orphan_parent_links: int = Field(ge=0)
    conflicting_parent_links: int = Field(ge=0)
    same_service_parent_links: int = Field(ge=0)
    cross_service_parent_links: int = Field(ge=0)
    client_server_pairs: int = Field(ge=0)
    producer_consumer_pairs: int = Field(ge=0)
    fallback_parent_pairs: int = Field(ge=0)
    service_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    strict_edge_count: int = Field(ge=0)
    fallback_only_edge_count: int = Field(ge=0)


@dataclass
class _EdgeAccumulator:
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    client_server_pairs: int = 0
    producer_consumer_pairs: int = 0
    cross_service_parent_pairs: int = 0
    evidence_candidates: list[tuple[tuple[object, ...], str]] = field(default_factory=list)
    trace_candidates: list[tuple[tuple[object, ...], str]] = field(default_factory=list)

    def add(
        self,
        *,
        evidence: RuntimeEdgeEvidence,
        child: TraceSpanObservation,
        parent: TraceSpanObservation,
        caller: str,
        callee: str,
    ) -> None:
        at = child.start_at
        self.first_seen = at if self.first_seen is None else min(self.first_seen, at)
        self.last_seen = at if self.last_seen is None else max(self.last_seen, at)
        if evidence is RuntimeEdgeEvidence.PAIRED_CLIENT_SERVER:
            self.client_server_pairs += 1
        elif evidence is RuntimeEdgeEvidence.PAIRED_PRODUCER_CONSUMER:
            self.producer_consumer_pairs += 1
        else:
            self.cross_service_parent_pairs += 1
        order = (at, caller, callee, parent.evidence_id, child.evidence_id)
        _bounded_unique_insert(self.evidence_candidates, order, parent.evidence_id)
        _bounded_unique_insert(self.evidence_candidates, order, child.evidence_id)
        _bounded_unique_insert(self.trace_candidates, order, child.trace_id)


def _bounded_unique_insert(
    values: list[tuple[tuple[object, ...], str]],
    order: tuple[object, ...],
    value: str,
) -> None:
    if any(existing == value for _key, existing in values):
        return
    values.append((order, value))
    values.sort(key=lambda item: (item[0], item[1]))
    del values[32:]


class RuntimeGraph:
    """Immutable lookup view over observed runtime service dependencies."""

    def __init__(
        self,
        services: Sequence[str],
        edges: Sequence[RuntimeServiceEdge],
        stats: RuntimeGraphStats,
    ) -> None:
        self.services = tuple(sorted(set(services)))
        self.edges = tuple(
            sorted(edges, key=lambda edge: (edge.caller_service, edge.callee_service))
        )
        self.stats = stats

    @classmethod
    def empty(cls) -> RuntimeGraph:
        return cls((), (), RuntimeGraphStats(**{field: 0 for field in _STAT_FIELDS}))

    def strict_edges(self) -> tuple[RuntimeServiceEdge, ...]:
        return tuple(edge for edge in self.edges if edge.has_strict_evidence)

    def fallback_edges(self) -> tuple[RuntimeServiceEdge, ...]:
        return tuple(edge for edge in self.edges if edge.fallback_only)

    def strict_outgoing(self, service: str) -> tuple[RuntimeServiceEdge, ...]:
        return tuple(edge for edge in self.strict_edges() if edge.caller_service == service)

    def strict_incoming(self, service: str) -> tuple[RuntimeServiceEdge, ...]:
        return tuple(edge for edge in self.strict_edges() if edge.callee_service == service)

    def observed_outgoing(self, service: str) -> tuple[RuntimeServiceEdge, ...]:
        return tuple(edge for edge in self.edges if edge.caller_service == service)

    def observed_incoming(self, service: str) -> tuple[RuntimeServiceEdge, ...]:
        return tuple(edge for edge in self.edges if edge.callee_service == service)


_STAT_FIELDS = (
    "input_spans",
    "canonical_spans",
    "duplicate_equivalent_rows",
    "conflicting_span_keys",
    "root_spans",
    "child_spans",
    "resolved_parent_links",
    "orphan_parent_links",
    "conflicting_parent_links",
    "same_service_parent_links",
    "cross_service_parent_links",
    "client_server_pairs",
    "producer_consumer_pairs",
    "fallback_parent_pairs",
    "service_count",
    "edge_count",
    "strict_edge_count",
    "fallback_only_edge_count",
)


def _in_window(at: datetime, start: datetime | None, end: datetime | None) -> bool:
    return (start is None or at >= start) and (end is None or at <= end)


def derive_runtime_graph_from_index(
    index: CanonicalTraceIndex,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> RuntimeGraph:
    """Build a runtime service graph from direct parent/span relationships."""
    canonical = index.spans
    conflicting = index.conflicting_keys

    edge_accumulators: dict[tuple[str, str], _EdgeAccumulator] = {}
    services: set[str] = set()
    root_spans = 0
    child_spans = 0
    resolved_parent_links = 0
    orphan_parent_links = 0
    conflicting_parent_links = 0
    same_service_parent_links = 0
    cross_service_parent_links = 0
    client_server_pairs = 0
    producer_consumer_pairs = 0
    fallback_parent_pairs = 0

    for child in canonical.values():
        if not _in_window(child.start_at, start, end):
            continue
        child_service = child.service.strip()
        services.add(child_service)
        if child.parent_span_id is None:
            root_spans += 1
            continue
        child_spans += 1
        parent_key = (child.trace_id, child.parent_span_id)
        if parent_key in conflicting:
            conflicting_parent_links += 1
            continue
        parent = canonical.get(parent_key)
        if parent is None:
            orphan_parent_links += 1
            continue
        resolved_parent_links += 1
        parent_service = parent.service.strip()
        services.add(parent_service)
        if parent_service == child_service:
            same_service_parent_links += 1
            continue
        cross_service_parent_links += 1
        evidence = classify_runtime_pair(parent, child)
        assert evidence is not None
        if evidence is RuntimeEdgeEvidence.PAIRED_CLIENT_SERVER:
            client_server_pairs += 1
        elif evidence is RuntimeEdgeEvidence.PAIRED_PRODUCER_CONSUMER:
            producer_consumer_pairs += 1
        else:
            fallback_parent_pairs += 1
        edge_key = (parent_service, child_service)
        accumulator = edge_accumulators.setdefault(edge_key, _EdgeAccumulator())
        accumulator.add(
            evidence=evidence,
            child=child,
            parent=parent,
            caller=parent_service,
            callee=child_service,
        )

    edges: list[RuntimeServiceEdge] = []
    for (caller, callee), accumulator in sorted(edge_accumulators.items()):
        assert accumulator.first_seen is not None
        assert accumulator.last_seen is not None
        edges.append(
            RuntimeServiceEdge(
                caller_service=caller,
                callee_service=callee,
                first_seen=accumulator.first_seen,
                last_seen=accumulator.last_seen,
                client_server_pairs=accumulator.client_server_pairs,
                producer_consumer_pairs=accumulator.producer_consumer_pairs,
                cross_service_parent_pairs=accumulator.cross_service_parent_pairs,
                evidence_ids=tuple(value for _order, value in accumulator.evidence_candidates),
                trace_ids=tuple(value for _order, value in accumulator.trace_candidates),
            )
        )
    strict_edge_count = sum(edge.has_strict_evidence for edge in edges)
    fallback_only_edge_count = sum(edge.fallback_only for edge in edges)
    stats = RuntimeGraphStats(
        input_spans=index.input_spans,
        canonical_spans=len(canonical),
        duplicate_equivalent_rows=index.duplicate_equivalent_rows,
        conflicting_span_keys=index.conflicting_span_keys,
        root_spans=root_spans,
        child_spans=child_spans,
        resolved_parent_links=resolved_parent_links,
        orphan_parent_links=orphan_parent_links,
        conflicting_parent_links=conflicting_parent_links,
        same_service_parent_links=same_service_parent_links,
        cross_service_parent_links=cross_service_parent_links,
        client_server_pairs=client_server_pairs,
        producer_consumer_pairs=producer_consumer_pairs,
        fallback_parent_pairs=fallback_parent_pairs,
        service_count=len(services),
        edge_count=len(edges),
        strict_edge_count=strict_edge_count,
        fallback_only_edge_count=fallback_only_edge_count,
    )
    return RuntimeGraph(tuple(services), edges, stats)


def derive_runtime_graph(
    spans: Sequence[TraceSpanObservation],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> RuntimeGraph:
    """Build a runtime graph after applying shared trace canonicalization."""
    return derive_runtime_graph_from_index(canonicalize_trace_spans(spans), start=start, end=end)


__all__ = [
    "CanonicalTraceIndex",
    "RuntimeEdgeEvidence",
    "RuntimeGraph",
    "RuntimeGraphStats",
    "RuntimeServiceEdge",
    "RuntimeSpanKind",
    "canonicalize_trace_spans",
    "classify_runtime_pair",
    "derive_runtime_graph",
    "derive_runtime_graph_from_index",
    "normalize_runtime_span_kind",
]
