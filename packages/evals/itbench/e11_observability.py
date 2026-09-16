"""Ground-truth-free observed-entity catalog and causal candidate retrieval.

This module is deliberately separate from the historical E9/E10 runtime.  It
turns structured snapshot observations into stable runtime-owned identities and
an explainable shortlist.  It never imports the evaluator or loads ground
truth; post-hoc callers may compare its frozen output separately.
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast

from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.dataset import iter_tsv
from packages.evals.itbench.snapshot_backend import classify_structured_log, normalize_trace_status


class SnapshotEvidenceSource(Protocol):
    """Minimal snapshot surface required by offline catalog construction."""

    scenario: Any

    def complete_source_records(
        self, category: ITBenchEvidenceCategory
    ) -> Iterable[dict[str, Any]]: ...

    def topology(
        self,
        *,
        entity: str | None = None,
        namespace: str | None = None,
        kind: str | None = None,
        pattern: str | None = None,
        relationship: str | None = None,
        limit: int | None = None,
    ) -> tuple[dict[str, Any], ...]: ...


E11_CATALOG_VERSION = "itbench_e11_observed_catalog_v2"
E11_RETRIEVAL_VERSION = "itbench_e11_evidence_retrieval_v2"
E11_DEFAULT_SHORTLIST_SIZE = 10
MAX_PRE_ONSET_SECONDS = 300
MAX_POST_ONSET_SECONDS = 300

_DIRECT_PROVENANCE = "DIRECT_K8S_OBJECT"
_EVENT_PROVENANCE = "DIRECT_K8S_EVENT"
_ALERT_PROVENANCE = "ALERT_LABEL"
_METRIC_PROVENANCE = "METRIC_RESOURCE"
_LOG_PROVENANCE = "LOG_RESOURCE"
_TRACE_PROVENANCE = "TRACE_RESOURCE"
_TOPOLOGY_PROVENANCE = "TOPOLOGY_DERIVED"


@dataclass(frozen=True, slots=True)
class E11RetrievalConfig:
    """One immutable retrieval policy shared by runtime and qualification."""

    # B1 keeps telemetry out of the ranked catalog, but semantic operations
    # resolve telemetry independently from the backend when evidence exists.
    include_telemetry_in_catalog: bool = False
    include_telemetry_in_ranking: bool = False
    use_direct_topology: bool = False
    use_causal_propagation: bool = True
    use_namespace_context: bool = True
    use_temporal: bool = False
    diversity: bool = False
    shortlist_size: int = E11_DEFAULT_SHORTLIST_SIZE

    def __post_init__(self) -> None:
        if self.shortlist_size < 1:
            raise ValueError("E11 shortlist size must be positive")


# This is the measured B1 configuration.  Telemetry ranking is disabled to
# preserve the measured B1 shortlist; semantic tools resolve their source
# availability independently from the backend.
E11_B1_CONFIG = E11RetrievalConfig()


@dataclass(slots=True)
class ObservedEntity:
    """One identity defensibly established by observable structured data."""

    canonical: str
    handle: str
    identity_type: str
    namespace: str
    kind: str
    name: str
    aliases: set[str] = field(default_factory=set)
    source_categories: set[str] = field(default_factory=set)
    provenance: set[str] = field(default_factory=set)
    evidence_refs: set[str] = field(default_factory=set)
    first_observed: str | None = None
    last_observed: str | None = None
    first_diagnostic_signal: str | None = None
    first_failure_signal: str | None = None
    first_anomaly_signal: str | None = None
    relationships: set[tuple[str, str, str]] = field(default_factory=set)
    identity_quality: str = "DIRECT"

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical,
            "handle": self.handle,
            "identity_type": self.identity_type,
            "namespace": self.namespace,
            "kind": self.kind,
            "name": self.name,
            "aliases": sorted(self.aliases),
            "source_categories": sorted(self.source_categories),
            "provenance": sorted(self.provenance),
            "evidence_refs": sorted(self.evidence_refs),
            "first_observed": self.first_observed,
            "last_observed": self.last_observed,
            "first_diagnostic_signal": self.first_diagnostic_signal,
            "first_failure_signal": self.first_failure_signal,
            "first_anomaly_signal": self.first_anomaly_signal,
            "relationships": [
                {"source": source, "target": target, "relationship": relation}
                for source, target, relation in sorted(self.relationships)
            ],
            "identity_quality": self.identity_quality,
        }


@dataclass(frozen=True, slots=True)
class RetrievalEvidence:
    """One inspectable contribution to a candidate score."""

    channel: str
    evidence_ref: str
    detail: str
    weight: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "evidence_ref": self.evidence_ref,
            "detail": self.detail,
            "weight": self.weight,
        }


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """A ranked runtime-owned candidate with fully explainable evidence."""

    handle: str
    canonical: str
    rank: int
    score: float
    family: str
    retrieval_evidence: tuple[RetrievalEvidence, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "canonical": self.canonical,
            "rank": self.rank,
            "score": round(self.score, 6),
            "family": self.family,
            "retrieval_evidence": [item.as_dict() for item in self.retrieval_evidence],
        }


class ObservedEntityCatalog:
    """Stable catalog separated from the bounded active shortlist."""

    def __init__(self, *, scenario_id: str) -> None:
        self.scenario_id = scenario_id
        self._entities: dict[str, ObservedEntity] = {}
        self._next_handle = 1
        self._ranking_revision = 0

    def add(
        self,
        *,
        canonical: str,
        identity_type: str,
        namespace: str,
        kind: str,
        name: str,
        source_category: str,
        provenance: str,
        evidence_ref: str,
        timestamp: str | None = None,
        aliases: Iterable[str] = (),
        identity_quality: str = "DIRECT",
        handle: str | None = None,
    ) -> ObservedEntity:
        """Add/merge an observed identity without inventing mappings."""
        entity = self._entities.get(canonical)
        if entity is None:
            assigned_handle = handle or f"C{self._next_handle:03d}"
            if self.by_handle(assigned_handle) is not None:
                raise ValueError(
                    f"catalog handle already belongs to another entity: {assigned_handle}"
                )
            entity = ObservedEntity(
                canonical=canonical,
                handle=assigned_handle,
                identity_type=identity_type,
                namespace=namespace,
                kind=kind,
                name=name,
                identity_quality=identity_quality,
            )
            numeric = int(assigned_handle[1:]) if assigned_handle.startswith("C") else 0
            self._next_handle = max(self._next_handle + 1, numeric + 1)
            self._entities[canonical] = entity
        elif entity.identity_type != identity_type:
            entity.identity_type = "|".join(
                sorted(set(entity.identity_type.split("|")) | {identity_type})
            )
            if entity.identity_quality == "DIRECT" and identity_quality != "DIRECT":
                entity.identity_quality = identity_quality
        entity.source_categories.add(source_category)
        entity.provenance.add(provenance)
        if evidence_ref:
            entity.evidence_refs.add(evidence_ref)
        entity.aliases.update(alias for alias in aliases if alias and alias != name)
        if timestamp:
            if entity.first_observed is None or timestamp < entity.first_observed:
                entity.first_observed = timestamp
            if entity.last_observed is None or timestamp > entity.last_observed:
                entity.last_observed = timestamp
        return entity

    def add_relationship(self, source: str, target: str, relationship: str) -> None:
        """Record a directed relation only when both endpoints are observed."""
        if source in self._entities and target in self._entities:
            self._entities[source].relationships.add((source, target, relationship))
            self._entities[target].relationships.add((source, target, relationship))
            self._entities[source].provenance.add(_TOPOLOGY_PROVENANCE)
            self._entities[target].provenance.add(_TOPOLOGY_PROVENANCE)

    def get(self, canonical: str) -> ObservedEntity | None:
        return self._entities.get(canonical)

    def by_handle(self, handle: str) -> ObservedEntity | None:
        return next((item for item in self._entities.values() if item.handle == handle), None)

    def entities(self) -> tuple[ObservedEntity, ...]:
        return tuple(sorted(self._entities.values(), key=lambda item: item.handle))

    def discover(self, items: Iterable[dict[str, Any]]) -> tuple[ObservedEntity, ...]:
        """Dynamically add defensible identities while preserving old handles."""
        discovered: list[ObservedEntity] = []
        for item in items:
            canonical = item.get("canonical")
            if not isinstance(canonical, str) or canonical in self._entities:
                continue
            required = (
                item.get("identity_type"),
                item.get("namespace"),
                item.get("kind"),
                item.get("name"),
            )
            if not all(isinstance(value, str) for value in required):
                continue
            discovered.append(
                self.add(
                    canonical=canonical,
                    identity_type=str(item["identity_type"]),
                    namespace=str(item["namespace"]),
                    kind=str(item["kind"]),
                    name=str(item["name"]),
                    source_category=str(item.get("source_category", "discovery")),
                    provenance=str(item.get("provenance", _TOPOLOGY_PROVENANCE)),
                    evidence_ref=str(item.get("evidence_ref", "")),
                    aliases=tuple(item.get("aliases", ())),
                    identity_quality=str(item.get("identity_quality", "MAPPED")),
                )
            )
        return tuple(discovered)

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog_version": E11_CATALOG_VERSION,
            "scenario_id": self.scenario_id,
            "entity_count": len(self._entities),
            "entities": [item.as_dict() for item in self.entities()],
        }


def build_observed_entity_catalog(
    backend: SnapshotEvidenceSource,
    *,
    include_telemetry: bool = True,
    max_telemetry_records: int = 5_000,
    telemetry_records: dict[ITBenchEvidenceCategory, tuple[dict[str, Any], ...]] | None = None,
) -> ObservedEntityCatalog:
    """Build a catalog from structured observations only.

    The function intentionally does not accept a ground-truth object.  The
    caller can serialize this result first and only then run post-hoc grading.
    """
    catalog = ObservedEntityCatalog(scenario_id=backend.scenario.scenario_id)
    for item in backend.complete_source_records(ITBenchEvidenceCategory.K8S_OBJECTS):
        identity = _k8s_identity(item)
        if identity:
            catalog.add(
                **identity,
                source_category=ITBenchEvidenceCategory.K8S_OBJECTS.value,
                provenance=_DIRECT_PROVENANCE,
            )
    for item in backend.complete_source_records(ITBenchEvidenceCategory.K8S_EVENTS):
        identity = _k8s_identity(item, event=True)
        if identity:
            catalog.add(
                **identity,
                source_category=ITBenchEvidenceCategory.K8S_EVENTS.value,
                provenance=_EVENT_PROVENANCE,
            )
    for item in backend.complete_source_records(ITBenchEvidenceCategory.ALERTS):
        record = item.get("record", {})
        labels = record.get("labels", {}) if isinstance(record, dict) else {}
        alert_name = str(labels.get("alertname", "")) if isinstance(labels, dict) else ""
        if alert_name.casefold() in {"watchdog", "infoinhibitor"}:
            continue
        for identity in _alert_identities(item):
            catalog.add(
                **identity,
                source_category=ITBenchEvidenceCategory.ALERTS.value,
                provenance=_ALERT_PROVENANCE,
            )
    if include_telemetry:
        for category, provenance in (
            (ITBenchEvidenceCategory.METRICS, _METRIC_PROVENANCE),
            (ITBenchEvidenceCategory.LOGS, _LOG_PROVENANCE),
            (ITBenchEvidenceCategory.TRACES, _TRACE_PROVENANCE),
        ):
            records = (
                telemetry_records.get(category, ())
                if telemetry_records is not None
                else _fast_source_records(backend, category, limit=max_telemetry_records)
            )
            for item in records:
                for identity in _telemetry_identities(item, category):
                    catalog.add(**identity, source_category=category.value, provenance=provenance)
            if category is ITBenchEvidenceCategory.TRACES:
                _add_trace_relationships(catalog, records)
    for edge in backend.topology(limit=None):
        catalog.add_relationship(edge["source"], edge["target"], edge["relationship"])
    _add_selector_relationships(backend, catalog)
    _add_namespace_relationships(catalog)
    return catalog


def rank_observed_candidates(
    backend: SnapshotEvidenceSource,
    catalog: ObservedEntityCatalog,
    *,
    limit: int = E11_DEFAULT_SHORTLIST_SIZE,
    diversity: bool = True,
    include_telemetry: bool = True,
    max_telemetry_records: int = 5_000,
    use_topology: bool = True,
    use_direct_topology: bool | None = None,
    use_causal_propagation: bool = True,
    use_namespace_context: bool = True,
    use_temporal: bool = True,
    telemetry_records: dict[ITBenchEvidenceCategory, tuple[dict[str, Any], ...]] | None = None,
    runtime_signals: dict[str, tuple[RetrievalEvidence, ...]] | None = None,
) -> tuple[RankedCandidate, ...]:
    """Rank candidates using evidence channels, never static object priors."""
    if limit < 1:
        raise ValueError("candidate limit must be positive")
    if use_direct_topology is not None:
        use_topology = use_direct_topology
    evidence: dict[str, list[RetrievalEvidence]] = defaultdict(list)
    onset = _incident_onset(backend)
    aliases: dict[str, set[str]] = defaultdict(set)
    for entity in catalog.entities():
        aliases[entity.canonical].update(
            {entity.name.casefold(), *[item.casefold() for item in entity.aliases]}
        )

    for item in backend.complete_source_records(ITBenchEvidenceCategory.ALERTS):
        if not _is_diagnostic_alert(item):
            continue
        alert_record = item.get("record", {})
        alert_labels = alert_record.get("labels", {}) if isinstance(alert_record, dict) else {}
        for identity in _alert_identities(item):
            _add_signal(
                catalog,
                evidence,
                identity["canonical"],
                "ALERT_LINK",
                item["evidence_id"],
                "explicit alert resource label",
                6.0,
                timestamp=_item_timestamp(item),
            )
        namespace = alert_labels.get("namespace") if isinstance(alert_labels, dict) else None
        namespace_entity = (
            catalog.get(f"_cluster/Namespace/{namespace}")
            if isinstance(namespace, str) and namespace
            else None
        )
        if namespace_entity is not None:
            _add_signal(
                catalog,
                evidence,
                namespace_entity.canonical,
                "NAMESPACE_ALERT",
                item["evidence_id"],
                "alert explicitly names the observed namespace",
                4.0,
            )

    for item in backend.complete_source_records(ITBenchEvidenceCategory.K8S_EVENTS):
        event_identity = _k8s_identity(item, event=True)
        if not event_identity:
            continue
        category = _event_category(item)
        if category == "NORMAL_INFORMATIONAL":
            continue
        weight = {
            "FAILURE": 8.0,
            "WARNING": 6.0,
            "BACKOFF": 8.0,
            "RESTART": 7.0,
            "OOM": 9.0,
            "EVICTION": 8.0,
            "SCHEDULING_FAILURE": 8.0,
            "MOUNT_FAILURE": 8.0,
            "IMAGE_FAILURE": 8.0,
            "RESOURCE_PRESSURE": 7.0,
            "CONFIG_CHANGE": 2.0,
            "DISRUPTION": 14.0,
            "NORMAL_INFORMATIONAL": 0.25,
        }.get(category, 0.5)
        _add_signal(
            catalog,
            evidence,
            event_identity["canonical"],
            "DISRUPTION" if category == "DISRUPTION" else "FAILURE_EVENT",
            item["evidence_id"],
            f"{category.lower()} Kubernetes event",
            weight,
            timestamp=_item_timestamp(item),
        )

    if include_telemetry:
        metric_anomalies = _metric_anomaly_signals(
            backend,
            catalog,
            limit=max_telemetry_records,
            records=None
            if telemetry_records is None
            else telemetry_records.get(ITBenchEvidenceCategory.METRICS, ()),
        )
        for canonical, (ref, detail, weight, timestamp) in metric_anomalies.items():
            _add_signal(
                catalog,
                evidence,
                canonical,
                "METRIC_ANOMALY",
                ref,
                detail,
                weight,
                timestamp=timestamp,
            )

        for category, channel in (
            (ITBenchEvidenceCategory.LOGS, "LOG_FAILURE"),
            (ITBenchEvidenceCategory.TRACES, "TRACE_ERROR_PROPAGATION"),
        ):
            records = (
                telemetry_records.get(category, ())
                if telemetry_records is not None
                else _fast_source_records(backend, category, limit=max_telemetry_records)
            )
            for item in records:
                telemetry_identity = _telemetry_identities(item, category)
                if not telemetry_identity:
                    continue
                record = item.get("record", {})
                if not isinstance(record, dict):
                    continue
                log_status, log_reason = classify_structured_log(record)
                trace_status, trace_reason = normalize_trace_status(record)
                error = (
                    log_status == "ERROR"
                    if category is ITBenchEvidenceCategory.LOGS
                    else trace_status == "ERROR"
                )
                if error:
                    _add_signal(
                        catalog,
                        evidence,
                        telemetry_identity[0]["canonical"],
                        channel,
                        item["evidence_id"],
                        f"structured telemetry: {log_reason if category is ITBenchEvidenceCategory.LOGS else trace_reason}",
                        5.0,
                        timestamp=_item_timestamp(item),
                    )

    for entity in catalog.entities():
        first_signal = _first_evidence_time(entity, evidence)
        if use_temporal and evidence.get(entity.canonical) and onset and first_signal:
            delta = _time_delta_seconds(first_signal, onset)
            if delta is not None and -MAX_PRE_ONSET_SECONDS <= delta <= MAX_POST_ONSET_SECONDS:
                _add_signal(
                    catalog,
                    evidence,
                    entity.canonical,
                    "TEMPORAL_ALIGNMENT",
                    f"time:{entity.handle}",
                    _temporal_detail(delta),
                    3.0,
                )
        if evidence.get(entity.canonical):
            for source, target, relationship in entity.relationships if use_topology else ():
                other = target if source == entity.canonical else source
                if other in catalog._entities and relationship in {
                    "configuration_reference",
                    "owner",
                    "scales",
                    "policy_selects",
                    "calls",
                    "called_by",
                }:
                    _add_signal(
                        catalog,
                        evidence,
                        entity.canonical,
                        "DIRECTED_TOPOLOGY",
                        f"topology:{sha256(f'{source}|{target}|{relationship}'.encode()).hexdigest()[:12]}",
                        f"directed {relationship} relationship",
                        1.0,
                    )

    # Propagate only along explicit causal/dependency edges.  This makes a
    # chaos, policy, HPA, owner, or configuration candidate reachable from an
    # affected workload without rewarding generic graph centrality.
    edges = _catalog_edges(catalog) if use_causal_propagation else ()
    # Causal paths can span Pod → ReplicaSet → Deployment → ConfigMap.  Reach
    # a fixed point while deduplicating derived evidence so edge order cannot
    # hide a valid multi-hop path.
    for _ in range(len(edges) + 1):
        changed = False
        for source, target, relationship in edges:
            if relationship in {"configuration_reference", "owner"} or (
                relationship == "namespace_contains" and use_namespace_context
            ):
                changed |= _propagate_signals(
                    evidence, source=source, target=target, relationship=relationship
                )
            elif relationship == "scales":
                changed |= _propagate_signals(
                    evidence, source=target, target=source, relationship=relationship
                )
            elif relationship in {"selects", "selector", "policy_selects", "disrupts"}:
                changed |= _propagate_signals(
                    evidence, source=target, target=source, relationship=relationship
                )
            elif relationship == "calls":
                changed |= _propagate_signals(
                    evidence, source=target, target=source, relationship=relationship
                )
        if not changed:
            break

    scored: list[tuple[float, ObservedEntity, tuple[RetrievalEvidence, ...]]] = []
    for entity in catalog.entities():
        if runtime_signals and entity.canonical in runtime_signals:
            evidence[entity.canonical].extend(runtime_signals[entity.canonical])
        items = tuple(
            sorted(
                evidence.get(entity.canonical, ()),
                key=lambda item: (-item.weight, item.channel, item.evidence_ref),
            )
        )
        if not items:
            continue
        score = _bounded_evidence_score(items)
        scored.append((score, entity, items))
    scored.sort(key=lambda item: (-item[0], item[1].canonical))
    selected: list[tuple[float, ObservedEntity, tuple[RetrievalEvidence, ...]]] = []
    family_counts: dict[str, int] = defaultdict(int)
    for candidate in scored:
        family = _candidate_family(candidate[1])
        if diversity and family_counts[family] >= 3:
            continue
        selected.append(candidate)
        family_counts[family] += 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected.extend(candidate for candidate in scored if candidate not in selected)
        selected = selected[:limit]
    catalog._ranking_revision += 1
    return tuple(
        RankedCandidate(
            handle=entity.handle,
            canonical=entity.canonical,
            rank=index,
            score=score,
            family=_candidate_family(entity),
            retrieval_evidence=items,
        )
        for index, (score, entity, items) in enumerate(selected, start=1)
    )


def _bounded_evidence_score(items: tuple[RetrievalEvidence, ...]) -> float:
    """Prevent repeated telemetry samples from becoming causal evidence."""
    caps = {
        "ALERT_LINK": 12.0,
        "NAMESPACE_ALERT": 6.0,
        "FAILURE_EVENT": 9.0,
        "DISRUPTION": 14.0,
        "METRIC_ANOMALY": 7.0,
        "TRACE_ERROR_PROPAGATION": 10.0,
        "LOG_FAILURE": 8.0,
        "TEMPORAL_ALIGNMENT": 3.0,
        "DIRECTED_TOPOLOGY": 4.0,
        "CAUSAL_RELATION": 18.0,
        "NAMESPACE_CONTEXT": 6.0,
    }
    by_channel: dict[str, float] = defaultdict(float)
    seen_evidence: set[tuple[str, str]] = set()
    for item in items:
        # The same bounded evidence reference is not independent causal
        # evidence.  Distinct incident events may still accumulate up to the
        # channel cap, while duplicate rows cannot inflate a score.
        key = (item.channel, item.evidence_ref)
        if key in seen_evidence:
            continue
        seen_evidence.add(key)
        by_channel[item.channel] += item.weight
    score = sum(min(value, caps.get(channel, value)) for channel, value in by_channel.items())
    independent_channels = {
        channel
        for channel in by_channel
        if channel not in {"DIRECTED_TOPOLOGY", "TEMPORAL_ALIGNMENT"}
    }
    # Independent observed channels are useful corroboration.  The bonus is
    # deliberately small and bounded; repeated samples remain capped above.
    return score + min(max(0, len(independent_channels) - 1) * 2.0, 6.0)


def _propagate_signals(
    evidence: dict[str, list[RetrievalEvidence]], *, source: str, target: str, relationship: str
) -> bool:
    if not evidence.get(source):
        return False
    independent_channels = {
        item.channel
        for item in evidence[source]
        if item.channel
        not in {
            "DIRECTED_TOPOLOGY",
            "TEMPORAL_ALIGNMENT",
            "NAMESPACE_CONTEXT",
            "CAUSAL_RELATION",
        }
    }
    corroborated = len(independent_channels) >= 2
    base_weights = {
        "configuration_reference": (18.0, 8.0),
        "owner": (12.0, 6.0),
        "scales": (14.0, 8.0),
        "selects": (14.0, 8.0),
        "selector": (14.0, 8.0),
        "policy_selects": (14.0, 8.0),
        "disrupts": (16.0, 10.0),
        "calls": (10.0, 5.0),
        "namespace_contains": (2.0, 2.0),
    }
    strong, weak = base_weights.get(relationship, (3.0, 3.0))
    weight = strong if corroborated else weak
    evidence_ref = (
        f"derived:{sha256(f'{source}|{target}|{relationship}'.encode()).hexdigest()[:12]}"
    )
    channel = "NAMESPACE_CONTEXT" if relationship == "namespace_contains" else "CAUSAL_RELATION"
    updated = RetrievalEvidence(
        channel,
        evidence_ref,
        f"{relationship} endpoint inherits incident signal from {source} "
        f"({len(independent_channels)} independent channels)",
        weight,
    )
    for index, item in enumerate(evidence[target]):
        if item.evidence_ref != evidence_ref:
            continue
        if weight > item.weight:
            evidence[target][index] = updated
            return True
        return False
    evidence[target].append(updated)
    return True


def _catalog_edges(catalog: ObservedEntityCatalog) -> tuple[tuple[str, str, str], ...]:
    edges: set[tuple[str, str, str]] = set()
    for entity in catalog.entities():
        edges.update(entity.relationships)
    return tuple(sorted(edges))


def _add_selector_relationships(
    backend: SnapshotEvidenceSource, catalog: ObservedEntityCatalog
) -> None:
    """Resolve explicit Chaos/Policy selectors without free-text identity guesses."""
    objects: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for item in backend.complete_source_records(ITBenchEvidenceCategory.K8S_OBJECTS):
        record = item.get("record", {})
        body = _json_body(record.get("Body")) if isinstance(record, dict) else None
        if not body or not isinstance(body.get("metadata"), dict):
            continue
        metadata = body["metadata"]
        kind = body.get("kind")
        name = metadata.get("name")
        if not isinstance(kind, str) or not isinstance(name, str):
            continue
        namespace = metadata.get("namespace", "_cluster")
        source = f"{namespace}/{kind}/{name}"
        objects.append((source, body, metadata))
    for source, body, metadata in objects:
        kind = str(body.get("kind", ""))
        if kind not in {
            "NetworkChaos",
            "PodChaos",
            "StressChaos",
            "JVMChaos",
            "Schedule",
            "NetworkPolicy",
        }:
            continue
        spec = cast(dict[str, Any], body.get("spec") if isinstance(body.get("spec"), dict) else {})
        selector = spec.get("selector") if isinstance(spec.get("selector"), dict) else {}
        match_labels = selector.get("matchLabels", {}) if isinstance(selector, dict) else {}
        namespaces = selector.get("namespaces", []) if isinstance(selector, dict) else []
        if not isinstance(match_labels, dict):
            match_labels = {}
        if not match_labels and not namespaces:
            # An empty selector is not evidence-backed identity mapping.
            continue
        for target, _target_body, target_metadata in objects:
            if target == source or target_metadata.get("namespace", "_cluster") not in (
                namespaces or [metadata.get("namespace", "_cluster")]
            ):
                continue
            labels = target_metadata.get("labels", {})
            if not isinstance(labels, dict) or not all(
                labels.get(key) == value for key, value in match_labels.items()
            ):
                continue
            if catalog.get(source) and catalog.get(target):
                catalog.add_relationship(
                    source, target, "disrupts" if "Chaos" in kind else "selects"
                )


def _add_namespace_relationships(catalog: ObservedEntityCatalog) -> None:
    """Connect namespaced observations to an observed Namespace object."""
    namespaces = {
        entity.name: entity.canonical for entity in catalog.entities() if entity.kind == "Namespace"
    }
    for entity in catalog.entities():
        namespace = namespaces.get(entity.namespace)
        if namespace and namespace != entity.canonical:
            catalog.add_relationship(entity.canonical, namespace, "namespace_contains")


def _add_trace_relationships(
    catalog: ObservedEntityCatalog, records: Iterable[dict[str, Any]]
) -> None:
    """Add exact caller→callee edges from structured trace resource fields."""
    for item in records:
        trace_identities = _telemetry_identities(item, ITBenchEvidenceCategory.TRACES)
        caller = next(
            (entry for entry in trace_identities if entry["identity_type"] == "ServiceIdentity"),
            None,
        )
        if caller is None:
            continue
        record = item.get("record", {})
        attributes = _mapping(record.get("SpanAttributes")) if isinstance(record, dict) else {}
        target_name = attributes.get("peer.service") or attributes.get("server.address")
        if not isinstance(target_name, str) or not target_name:
            continue
        target = f"{caller['namespace']}/Service/{target_name}"
        if catalog.get(caller["canonical"]) and catalog.get(target) is None:
            catalog.add(
                canonical=target,
                identity_type="ServiceIdentity",
                namespace=caller["namespace"],
                kind="Service",
                name=target_name,
                source_category=ITBenchEvidenceCategory.TRACES.value,
                provenance=_TRACE_PROVENANCE,
                evidence_ref=str(item.get("evidence_id", "")),
                timestamp=_item_timestamp(item),
                aliases=(target_name,),
                identity_quality="MAPPED",
            )
        if catalog.get(caller["canonical"]) and catalog.get(target):
            catalog.add_relationship(caller["canonical"], target, "calls")


def _add_signal(
    catalog: ObservedEntityCatalog,
    evidence: dict[str, list[RetrievalEvidence]],
    canonical: str,
    channel: str,
    evidence_ref: str,
    detail: str,
    weight: float,
    *,
    timestamp: str | None = None,
) -> None:
    if catalog.get(canonical) is not None:
        if not any(
            item.channel == channel and item.evidence_ref == evidence_ref
            for item in evidence[canonical]
        ):
            evidence[canonical].append(
                RetrievalEvidence(channel, evidence_ref, detail[:240], weight)
            )
        entity = catalog.get(canonical)
        if entity and timestamp:
            if entity.first_observed is None or timestamp < entity.first_observed:
                entity.first_observed = timestamp
            if entity.last_observed is None or timestamp > entity.last_observed:
                entity.last_observed = timestamp
            if channel not in {"DIRECTED_TOPOLOGY", "NAMESPACE_CONTEXT"}:
                if (
                    entity.first_diagnostic_signal is None
                    or timestamp < entity.first_diagnostic_signal
                ):
                    entity.first_diagnostic_signal = timestamp
            if channel in {"FAILURE_EVENT", "DISRUPTION", "LOG_FAILURE", "TRACE_ERROR_PROPAGATION"}:
                if entity.first_failure_signal is None or timestamp < entity.first_failure_signal:
                    entity.first_failure_signal = timestamp
            if channel == "METRIC_ANOMALY":
                if entity.first_anomaly_signal is None or timestamp < entity.first_anomaly_signal:
                    entity.first_anomaly_signal = timestamp


def _k8s_identity(item: dict[str, Any], *, event: bool = False) -> dict[str, Any] | None:
    record = item.get("record", {})
    body = _json_body(record.get("Body")) if isinstance(record, dict) else None
    body = body or (record if isinstance(record, dict) else {})
    if event and isinstance(body.get("object"), dict):
        event_body = body["object"]
        candidate = event_body.get("involvedObject") or event_body.get("regarding") or event_body
    elif event and isinstance(body.get("involvedObject"), dict):
        candidate = body["involvedObject"]
    elif event and isinstance(body.get("regarding"), dict):
        candidate = body["regarding"]
    else:
        candidate = body.get("object") if isinstance(body.get("object"), dict) else body
    if not isinstance(candidate, dict):
        return None
    metadata = cast(
        dict[str, Any],
        candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else candidate,
    )
    kind = candidate.get("kind") or metadata.get("kind")
    name = metadata.get("name")
    namespace = metadata.get("namespace", "_cluster")
    if kind == "Event" or not all(isinstance(value, str) and value for value in (kind, name)):
        return None
    namespace = namespace if isinstance(namespace, str) and namespace else "_cluster"
    return {
        "canonical": f"{namespace}/{kind}/{name}",
        "identity_type": "KubernetesEntity",
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "evidence_ref": str(item.get("evidence_id", "")),
        "timestamp": _item_timestamp(item),
        "aliases": _structured_aliases(metadata),
    }


def _alert_identities(item: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    record = item.get("record", {})
    labels = record.get("labels", {}) if isinstance(record, dict) else {}
    if not isinstance(labels, dict):
        return ()
    namespace = str(labels.get("namespace", labels.get("k8s.namespace.name", "_cluster")))
    result: list[dict[str, Any]] = []
    for key, kind in (
        ("pod", "Pod"),
        ("workload", "Deployment"),
        ("deployment", "Deployment"),
        ("statefulset", "StatefulSet"),
        ("daemonset", "DaemonSet"),
        ("service", "Service"),
        ("service_name", "Service"),
        ("job", "Job"),
        ("node", "Node"),
    ):
        value = labels.get(key)
        if isinstance(value, str) and value:
            ns = "_cluster" if kind == "Node" else namespace
            result.append(
                {
                    "canonical": f"{ns}/{kind}/{value}",
                    "identity_type": "KubernetesEntity",
                    "namespace": ns,
                    "kind": kind,
                    "name": value,
                    "evidence_ref": str(item.get("evidence_id", "")),
                    "timestamp": _item_timestamp(item),
                    "aliases": (value,),
                }
            )
    return tuple(result)


# Recurring control-plane/telemetry health alerts, compared case-insensitively.
E11_BACKGROUND_ALERT_NAMES = frozenset(
    {
        "watchdog",
        "infoinhibitor",
        "prometheusnotconnectedtoalertmanagers",
        "kubeclientcertificateexpiration",
        "kubeschedulerdown",
        "kubecontrollermanagerdown",
    }
)


def _is_diagnostic_alert(item: dict[str, Any]) -> bool:
    """Exclude recurring control-plane/telemetry health noise from linkage."""
    record = item.get("record", {})
    labels = record.get("labels", {}) if isinstance(record, dict) else {}
    name = str(labels.get("alertname", "")).casefold() if isinstance(labels, dict) else ""
    return name not in E11_BACKGROUND_ALERT_NAMES


def _telemetry_identities(
    item: dict[str, Any], category: ITBenchEvidenceCategory
) -> tuple[dict[str, Any], ...]:
    record = item.get("record", {})
    if not isinstance(record, dict):
        return ()
    # Metric rows commonly carry pod/service fields directly.  Avoid parsing
    # a serialized label map for every high-volume sample unless direct fields
    # do not provide an identity.
    resource = (
        _mapping(record.get("ResourceAttributes"))
        if category is not ITBenchEvidenceCategory.METRICS
        else {}
    )
    tags = _mapping(record.get("tags")) if category is not ITBenchEvidenceCategory.METRICS else {}
    values = {**tags, **resource, **record}
    namespace = str(values.get("k8s.namespace.name", values.get("namespace", "_cluster")))
    result: list[dict[str, Any]] = []
    for key, kind in (
        ("k8s.pod.name", "Pod"),
        ("pod_name", "Pod"),
        ("k8s.deployment.name", "Deployment"),
        ("deployment", "Deployment"),
        ("k8s.statefulset.name", "StatefulSet"),
        ("k8s.daemonset.name", "DaemonSet"),
        ("k8s.node.name", "Node"),
        ("node", "Node"),
    ):
        value = values.get(key)
        if isinstance(value, str) and value:
            ns = "_cluster" if kind == "Node" else namespace
            result.append(
                {
                    "canonical": f"{ns}/{kind}/{value}",
                    "identity_type": "TelemetryResourceIdentity",
                    "namespace": ns,
                    "kind": kind,
                    "name": value,
                    "evidence_ref": str(item.get("evidence_id", "")),
                    "timestamp": _item_timestamp(item),
                    "aliases": (value,),
                }
            )
    if not result and category is ITBenchEvidenceCategory.METRICS:
        tags = _mapping(record.get("tags"))
        values = {**tags, **record}
        namespace = str(values.get("namespace", "_cluster"))
        for key, kind in (("pod", "Pod"), ("service", "Service")):
            value = values.get(key)
            if isinstance(value, str) and value:
                result.append(
                    {
                        "canonical": f"{namespace}/{kind}/{value}",
                        "identity_type": "TelemetryResourceIdentity",
                        "namespace": namespace,
                        "kind": kind,
                        "name": value,
                        "evidence_ref": str(item.get("evidence_id", "")),
                        "timestamp": _item_timestamp(item),
                        "aliases": (value,),
                    }
                )
    service = values.get(
        "service.name",
        values.get("service_name", values.get("ServiceName", values.get("service"))),
    )
    if isinstance(service, str) and service:
        result.append(
            {
                "canonical": f"{namespace}/Service/{service}",
                "identity_type": "ServiceIdentity",
                "namespace": namespace,
                "kind": "Service",
                "name": service,
                "evidence_ref": str(item.get("evidence_id", "")),
                "timestamp": _item_timestamp(item),
                "aliases": (service,),
            }
        )
    return tuple(result)


def _structured_aliases(metadata: dict[str, Any]) -> tuple[str, ...]:
    labels = metadata.get("labels", {})
    if not isinstance(labels, dict):
        return ()
    return tuple(
        str(value)
        for key, value in labels.items()
        if key in {"app", "app.kubernetes.io/name", "service", "workload"}
        and isinstance(value, str)
    )


def _json_body(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(value)
        except (SyntaxError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _item_timestamp(item: dict[str, Any]) -> str | None:
    record = item.get("record", {})
    if not isinstance(record, dict):
        return None
    for key in (
        "Timestamp",
        "TimestampTime",
        "timestamp",
        "activeAt",
        "startsAt",
        "lastTimestamp",
        "firstTimestamp",
    ):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    body = _json_body(record.get("Body"))
    if body:
        for key in ("lastTimestamp", "firstTimestamp", "eventTime", "creationTimestamp"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _incident_onset(backend: SnapshotEvidenceSource) -> str | None:
    timestamps = [
        _item_timestamp(item)
        for item in backend.complete_source_records(ITBenchEvidenceCategory.ALERTS)
        if _is_diagnostic_alert(item)
    ]
    return min((item for item in timestamps if item), default=None)


def _event_category(item: dict[str, Any]) -> str:
    record = item.get("record", {})
    body = _json_body(record.get("Body")) if isinstance(record, dict) else None
    if isinstance(body, dict) and isinstance(body.get("object"), dict):
        body = body["object"]
    body = body or {}
    reason = str(body.get("reason", ""))
    message = str(body.get("message", body.get("note", "")))
    text = f"{reason} {message}".casefold()
    if "chaos" in text and any(token in text for token in ("applied", "started", "spawned")):
        return "DISRUPTION"
    for category, tokens in (
        ("OOM", ("oom", "outofmemory")),
        ("EVICTION", ("evict",)),
        ("BACKOFF", ("backoff", "back-off")),
        ("RESTART", ("restart", "crashloop")),
        ("SCHEDULING_FAILURE", ("unschedul", "failedscheduling")),
        ("MOUNT_FAILURE", ("mount", "volume")),
        ("IMAGE_FAILURE", ("imagepull", "image pull")),
        ("RESOURCE_PRESSURE", ("pressure", "insufficient")),
        ("CONFIG_CHANGE", ("config", "reload")),
        ("FAILURE", ("fail", "error")),
    ):
        if any(token in text for token in tokens):
            return category
    return (
        "WARNING" if str(body.get("type", "")).casefold() == "warning" else "NORMAL_INFORMATIONAL"
    )


def _metric_anomaly_signals(
    backend: SnapshotEvidenceSource,
    catalog: ObservedEntityCatalog,
    *,
    limit: int,
    records: Iterable[dict[str, Any]] | None = None,
) -> dict[str, tuple[str, str, float, str | None]]:
    series: dict[str, list[tuple[str | None, float, str, str]]] = defaultdict(list)
    source = (
        records
        if records is not None
        else _fast_source_records(backend, ITBenchEvidenceCategory.METRICS, limit=limit)
    )
    for item in source:
        record = item.get("record", {})
        if not isinstance(record, dict):
            continue
        identity = _telemetry_identities(item, ITBenchEvidenceCategory.METRICS)
        if not identity:
            continue
        value = next(
            (
                record.get(key)
                for key in ("value", "Value", "metric_value")
                if record.get(key) is not None
            ),
            None,
        )
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        metric_name = str(record.get("metric_name", record.get("MetricName", "unknown")))
        metric_type = _metric_semantics(record, metric_name)
        if not _metric_is_anomaly_candidate(metric_name, metric_type):
            continue
        for identity_item in identity:
            if catalog.get(identity_item["canonical"]):
                series[
                    f"{identity_item['canonical']}|{metric_name}|{_metric_series_identity(record)}"
                ].append((_item_timestamp(item), number, item["evidence_id"], metric_type))
    result: dict[str, tuple[str, str, float, str | None]] = {}
    for key, values in series.items():
        if len(values) < 2:
            continue
        deduped = {
            (stamp, value, ref): (stamp, value, ref, kind) for stamp, value, ref, kind in values
        }
        ordered = sorted(deduped.values(), key=lambda item: (item[0] or "", item[2]))
        timestamps = tuple(item[0] for item in ordered if item[0] is not None)
        numbers = [item[1] for item in ordered]
        summary = _metric_summary_for_type(numbers, timestamps, ordered[0][3])
        if not summary["anomaly"]:
            continue
        canonical, metric_name, _label_hash = key.split("|", 2)
        score = min(7.0, 2.0 + abs(float(summary["relative_change"] or 0.0)) * 3.0)
        ref, detail, _score, timestamp = result.get(canonical, ("", "", -1.0, None))
        signal = (
            ordered[-1][2],
            f"{metric_name} {summary['direction']} ({summary['metric_type']})",
            score,
            ordered[-1][0],
        )
        if signal[2] > _score or (signal[2] == _score and signal[0] < ref):
            result[canonical] = signal
    return result


def _metric_semantics(record: dict[str, Any], metric_name: str) -> str:
    raw = record.get("metric_type", record.get("MetricType", record.get("type")))
    value = str(raw).casefold() if raw is not None else ""
    if value in {"counter", "cumulative", "monotonic"}:
        return "counter"
    if value in {"gauge", "value", "instant"}:
        return "gauge"
    if value in {"histogram", "bucket"} or metric_name.casefold().endswith("_bucket"):
        return "histogram"
    if value == "summary":
        return "summary"
    if metric_name.casefold().endswith("_total") or metric_name.casefold().endswith("_count"):
        return "counter"
    return "unknown"


def _metric_series_identity(record: dict[str, Any]) -> str:
    """Keep independently labelled metric series independent during ranking."""
    labels: dict[str, str] = {}
    for key in ("tags", "labels", "resource", "ResourceAttributes", "resource_attributes"):
        value = record.get(key)
        if isinstance(value, dict):
            labels.update({str(label): str(item) for label, item in value.items()})
    for key in (
        "service",
        "service_name",
        "service.name",
        "pod",
        "pod_name",
        "workload",
        "deployment",
        "namespace",
        "instance",
    ):
        if record.get(key) is not None:
            labels[key] = str(record[key])
    encoded = json.dumps(labels, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _metric_is_anomaly_candidate(metric_name: str, metric_type: str = "unknown") -> bool:
    """Exclude only non-diagnostic resource/histogram series before semantics."""
    name = metric_name.casefold()
    if metric_type in {"histogram", "summary"} or name.endswith("_bucket"):
        return False
    if "resource_requests" in name or "resource_limits" in name:
        return False
    return True


def _metric_summary_for_type(
    values: list[float], timestamps: tuple[str, ...], metric_type: str
) -> dict[str, Any]:
    ordered = (
        sorted(zip(timestamps, values, strict=True), key=lambda item: item[0])
        if len(timestamps) == len(values)
        else []
    )
    series = [value for _timestamp, value in ordered] if ordered else values
    summary = {
        "first": series[0],
        "last": series[-1],
        "relative_change": ((series[-1] - series[0]) / abs(series[0]) if series[0] else None),
        "direction": "increase"
        if series[-1] > series[0]
        else "decrease"
        if series[-1] < series[0]
        else "stable",
    }
    summary["metric_type"] = metric_type
    if metric_type != "counter":
        summary["anomaly"] = abs(float(summary.get("relative_change") or 0.0)) >= 0.20
        return summary
    ordered = (
        sorted(zip(timestamps, values, strict=True), key=lambda item: item[0])
        if len(timestamps) == len(values)
        else []
    )
    rates: list[float] = []
    for (before, before_value), (after, after_value) in zip(ordered, ordered[1:], strict=False):
        delta_seconds = _time_delta_seconds(after, before)
        if delta_seconds and delta_seconds > 0:
            rates.append((after_value - before_value) / delta_seconds)
    if len(rates) < 2:
        summary["rate_change"] = None
        summary["anomaly"] = False
        return summary
    baseline = sum(rates[: max(1, len(rates) // 2)]) / max(1, len(rates) // 2)
    incident = sum(rates[-max(1, len(rates) // 2) :]) / max(1, len(rates) // 2)
    change = incident - baseline
    summary["baseline_rate"] = baseline
    summary["incident_rate"] = incident
    summary["rate_change"] = change
    summary["relative_change"] = change / abs(baseline) if baseline else None
    summary["direction"] = "increase" if change > 0 else "decrease" if change < 0 else "stable"
    summary["anomaly"] = abs(change) >= max(0.001, abs(baseline) * 0.20)
    return summary


def _fast_source_records(
    backend: SnapshotEvidenceSource,
    category: ITBenchEvidenceCategory,
    *,
    limit: int | None = None,
) -> Iterable[dict[str, Any]]:
    """Read high-volume telemetry without constructing UUID envelopes per row."""
    root = backend.scenario.snapshot_path
    for relative_path in backend.scenario.evidence_files[category]:
        path = Path(root) / relative_path
        for row_index, row in enumerate(iter_tsv(path)):
            if limit is not None and row_index >= limit:
                break
            yield {
                "evidence_id": f"{category.value}:{relative_path}:{row_index}",
                "category": category.value,
                "source_file": relative_path,
                "row_index": row_index,
                "record": row,
            }


def _first_evidence_time(
    entity: ObservedEntity, evidence: dict[str, list[RetrievalEvidence]]
) -> str | None:
    return (
        entity.first_failure_signal or entity.first_anomaly_signal or entity.first_diagnostic_signal
    )


def _temporal_detail(delta: float) -> str:
    seconds = int(abs(delta))
    return f"signal occurred {seconds}s {'before' if delta < 0 else 'after/near'} incident onset"


def _time_delta_seconds(left: str, right: str) -> float | None:
    try:

        def parse(value: str) -> datetime:
            normalized = value.replace("Z", "+00:00").replace(" ", "T")
            parsed = datetime.fromisoformat(normalized)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

        return (parse(left) - parse(right)).total_seconds()
    except (TypeError, ValueError):
        return None


def _candidate_family(entity: ObservedEntity) -> str:
    return f"{entity.namespace}/{entity.kind}"


__all__ = [
    "E11_B1_CONFIG",
    "E11_BACKGROUND_ALERT_NAMES",
    "E11_CATALOG_VERSION",
    "E11RetrievalConfig",
    "E11_RETRIEVAL_VERSION",
    "E11_DEFAULT_SHORTLIST_SIZE",
    "ObservedEntity",
    "ObservedEntityCatalog",
    "RankedCandidate",
    "RetrievalEvidence",
    "build_observed_entity_catalog",
    "rank_observed_candidates",
]
