"""Ground-truth-separated causal-model completeness diagnostics.

The blind half of this module observes only published snapshot data and the
ordinary RCA objects.  Ground-truth matching is deliberately kept in the
CLI overlay so the pipeline inventory cannot depend on evaluator labels.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, cast

from packages.evals.causal_semantics import finding_key
from packages.rca.engine import build_case, diagnose_case
from packages.rca.model import (
    Candidate,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    Hypothesis,
    Resolution,
)
from packages.rca.resolution import _has_aligned_initiating
from packages.rca.source import ObservationSource

_FORBIDDEN_BLIND_KEYS = frozenset(
    {"ground_truth", "expected", "correct", "root_cause", "answer", "label"}
)


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _canonical(value: EntityRef | str) -> str:
    return value.canonical if isinstance(value, EntityRef) else value


def _parse_canonical(value: str) -> tuple[str, str, str]:
    namespace, kind, name = value.split("/", 2)
    return namespace, kind, name


@dataclass(frozen=True)
class ObjectObservationSnapshot:
    evidence_id: str
    observed_at: str | None
    lifecycle: str | None
    changed_from_previous: bool | None


@dataclass(frozen=True)
class EventObservationSnapshot:
    evidence_id: str
    reason: str
    type: str
    first_at: str | None
    last_at: str | None
    count: int


@dataclass(frozen=True)
class SnapshotEntitySnapshot:
    canonical: str
    namespace: str
    kind: str
    name: str
    object_record_count: int
    event_record_count: int
    topology_neighbors: tuple[str, ...]
    topology_relations: tuple[str, ...]
    object_versions: tuple[ObjectObservationSnapshot, ...]
    event_summaries: tuple[EventObservationSnapshot, ...]
    truncated: bool = False


@dataclass(frozen=True)
class SourceExposureSnapshot:
    history_entities: tuple[str, ...]
    event_entities: tuple[str, ...]
    traffic_entities: tuple[str, ...]
    alert_services: tuple[str, ...]
    history_counts: tuple[tuple[str, int], ...] = ()
    history_lifecycles: tuple[tuple[str, tuple[str, ...]], ...] = ()
    event_counts: tuple[tuple[str, int], ...] = ()
    traffic_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class CatalogConsistency:
    source_k8s_entity_count: int
    observable_catalog_count: int
    missing_from_catalog: tuple[str, ...]
    consistent: bool

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(asdict(self)))


@dataclass(frozen=True)
class FindingSnapshot:
    finding_key: str
    kind: str
    entity: str
    related_entities: tuple[str, ...]
    temporal_role: str
    at: str | None
    onset_delta_seconds: float | None
    evidence_ids: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class CandidateSnapshot:
    entity: str
    finding_keys: tuple[str, ...]
    score: float
    causal_explanation: str
    linked_symptoms: tuple[str, ...]


@dataclass(frozen=True)
class HypothesisSnapshot:
    hypothesis_id: str
    causal_actor: str
    members: tuple[str, ...]
    manifestations: tuple[str, ...]
    finding_keys: tuple[str, ...]
    initiating_finding_keys: tuple[str, ...]
    supporting_finding_keys: tuple[str, ...]
    contradictory_finding_keys: tuple[str, ...]
    causal_explanation: str
    causal_paths: tuple[tuple[tuple[str, str, str], ...], ...]
    plausible: bool
    plausibility_reasons: tuple[str, ...]
    leading: bool
    verification_decision: str | None


@dataclass(frozen=True)
class EliminationMechanics:
    hypothesis_id: str
    causal_actor: str
    reason_codes: tuple[str, ...]
    causal_explanation: str
    has_causal_symptom_link: bool
    has_aligned_initiating_evidence: bool
    initiating_findings: tuple[str, ...]
    initiating_deltas: tuple[float, ...]
    contradictory_findings: tuple[str, ...]
    contradiction_deltas: tuple[float, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class CausalCompletenessBlindAudit:
    incident_id: str
    snapshot_entities: tuple[SnapshotEntitySnapshot, ...]
    source_exposure: SourceExposureSnapshot
    catalog_consistency: CatalogConsistency
    findings: tuple[FindingSnapshot, ...]
    candidates: tuple[CandidateSnapshot, ...]
    hypotheses: tuple[HypothesisSnapshot, ...]
    elimination_mechanics: tuple[EliminationMechanics, ...]
    resolution: str
    selected_hypothesis_id: str | None
    selected_actor: str | None

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(asdict(self)))


@dataclass(frozen=True)
class RootCauseJourney:
    root_group_id: str
    observable_snapshot_matches: tuple[str, ...]
    source_exposed_matches: tuple[str, ...]
    direct_finding_matches: tuple[str, ...]
    related_finding_matches: tuple[str, ...]
    candidate_matches: tuple[str, ...]
    hypothesis_matches: tuple[str, ...]
    plausible_hypothesis_matches: tuple[str, ...]
    leading_hypothesis_matches: tuple[str, ...]
    first_loss_stage: str
    final_stage: str
    elimination_details: tuple[EliminationMechanics, ...]
    extraction_diagnostic: str | None = None
    elimination_diagnostic: str | None = None
    manifestation_matches: tuple[str, ...] = ()
    source_match_origins: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(asdict(self)))


@dataclass(frozen=True)
class ScenarioCompletenessOverlay:
    scenario_id: str
    production_resolution: str
    root_journeys: tuple[RootCauseJourney, ...]
    scenario_primary_loss_stage: str

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _json_value(asdict(self)))


def _object_snapshot(
    record: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> ObjectObservationSnapshot:
    body = record.get("Body", {})
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = {}
    body = body if isinstance(body, dict) else {}
    previous_body = previous.get("Body", {}) if previous else None
    if isinstance(previous_body, str):
        try:
            previous_body = json.loads(previous_body)
        except ValueError:
            previous_body = {}
    changed = None if previous is None else body != previous_body
    # Snapshot metadata is not the production-derived Lifecycle contract.
    # Lifecycle is recorded from SourceExposureSnapshot instead.
    lifecycle = None
    return ObjectObservationSnapshot(
        evidence_id=str(record.get("evidence_id", "")),
        observed_at=str(record.get("Timestamp")) if record.get("Timestamp") else None,
        lifecycle=str(lifecycle) if lifecycle else None,
        changed_from_previous=changed,
    )


def _event_snapshot(record: Mapping[str, Any]) -> EventObservationSnapshot:
    body: Any = record.get("Body", {})
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = {}
    body = body.get("object", body) if isinstance(body, dict) else {}
    body = body if isinstance(body, dict) else {}
    first = body.get("firstTimestamp") or body.get("eventTime")
    last = body.get("lastTimestamp") or first
    return EventObservationSnapshot(
        evidence_id=str(record.get("evidence_id", "")),
        reason=str(body.get("reason", "")),
        type=str(body.get("type", "Normal")),
        first_at=str(first) if first else None,
        last_at=str(last) if last else None,
        count=int(body.get("count") or 1),
    )


def snapshot_entity_inventory(
    backend: Any, *, limit: int = 100
) -> tuple[SnapshotEntitySnapshot, ...]:
    """Capture the complete structured entity catalog and bounded contexts."""
    entities = backend.observable_entities()
    from packages.evals.itbench.contracts import ITBenchEvidenceCategory
    from packages.evals.itbench.snapshot_backend import _record_entity

    object_records = tuple(backend.complete_source_records(ITBenchEvidenceCategory.K8S_OBJECTS))
    event_records = tuple(backend.complete_source_records(ITBenchEvidenceCategory.K8S_EVENTS))
    objects_by_entity: dict[str, list[Mapping[str, Any]]] = {}
    events_by_entity: dict[str, list[Mapping[str, Any]]] = {}
    for record in object_records:
        entity = _record_entity(cast(dict[str, Any], record))
        if entity is not None:
            objects_by_entity.setdefault(entity.canonical, []).append(record)
    for record in event_records:
        entity = _record_entity(cast(dict[str, Any], record))
        if entity is not None:
            events_by_entity.setdefault(entity.canonical, []).append(record)
    all_edges = tuple(backend.topology(limit=None))
    result: list[SnapshotEntitySnapshot] = []
    for item in entities:
        canonical = f"{item['namespace']}/{item['kind']}/{item['name']}"
        entity_objects = tuple(objects_by_entity.get(canonical, ()))[:limit]
        entity_events = tuple(events_by_entity.get(canonical, ()))[:limit]
        previous: Mapping[str, Any] | None = None
        object_versions: list[ObjectObservationSnapshot] = []
        for record in entity_objects:
            object_versions.append(_object_snapshot(record, previous))
            previous = record
        event_summaries = tuple(_event_snapshot(record) for record in entity_events)
        neighbors: set[str] = set()
        relations: set[str] = set()
        for edge in all_edges:
            source = str(edge.get("source", ""))
            target = str(edge.get("target", ""))
            if source == canonical:
                neighbors.add(target)
                relations.add(f"{source}|{edge.get('relationship', '')}|{target}")
            elif target == canonical:
                neighbors.add(source)
                relations.add(f"{source}|{edge.get('relationship', '')}|{target}")
        result.append(
            SnapshotEntitySnapshot(
                canonical=canonical,
                namespace=str(item["namespace"]),
                kind=str(item["kind"]),
                name=str(item["name"]),
                object_record_count=len(objects_by_entity.get(canonical, ())),
                event_record_count=len(events_by_entity.get(canonical, ())),
                topology_neighbors=tuple(sorted(neighbors)),
                topology_relations=tuple(sorted(relations)),
                object_versions=tuple(object_versions),
                event_summaries=event_summaries,
                truncated=len(entity_objects) < len(objects_by_entity.get(canonical, ()))
                or len(entity_events) < len(events_by_entity.get(canonical, ())),
            )
        )
    return tuple(sorted(result, key=lambda item: item.canonical))


def source_exposure_inventory(source: ObservationSource) -> SourceExposureSnapshot:
    history = source.object_history()
    events = tuple(source.events())
    traffic = tuple(source.traffic_observations())
    alerts = tuple(source.alerts())
    history_counts = tuple(
        sorted((entity.canonical, len(versions)) for entity, versions in history.items())
    )
    history_lifecycles = tuple(
        sorted(
            (
                entity.canonical,
                tuple(
                    sorted({version.lifecycle.value for version in versions if version.lifecycle})
                ),
            )
            for entity, versions in history.items()
        )
    )
    event_counts: dict[str, int] = {}
    for event in events:
        event_counts[event.entity.canonical] = event_counts.get(event.entity.canonical, 0) + 1
    traffic_counts: dict[str, int] = {}
    for observation in traffic:
        traffic_counts[observation.entity.canonical] = (
            traffic_counts.get(observation.entity.canonical, 0) + 1
        )
    alert_services = tuple(sorted({str(alert.service) for alert in alerts if alert.service}))
    return SourceExposureSnapshot(
        history_entities=tuple(sorted(entity.canonical for entity in history)),
        event_entities=tuple(sorted(event_counts)),
        traffic_entities=tuple(sorted(traffic_counts)),
        alert_services=alert_services,
        history_counts=history_counts,
        history_lifecycles=history_lifecycles,
        event_counts=tuple(sorted(event_counts.items())),
        traffic_counts=tuple(sorted(traffic_counts.items())),
    )


def _finding_snapshot(finding: Finding) -> FindingSnapshot:
    return FindingSnapshot(
        finding_key=finding_key(finding),
        kind=finding.kind.value,
        entity=finding.entity.canonical,
        related_entities=tuple(sorted(item.canonical for item in finding.related)),
        temporal_role=finding.temporal_role.value,
        at=finding.at.isoformat() if finding.at else None,
        onset_delta_seconds=finding.onset_delta_seconds,
        evidence_ids=tuple(sorted(finding.evidence_ids)),
        summary=finding.summary,
    )


def _candidate_snapshot(candidate: Candidate) -> CandidateSnapshot:
    return CandidateSnapshot(
        entity=candidate.entity.canonical,
        finding_keys=tuple(sorted(finding_key(item) for item in candidate.findings)),
        score=candidate.score,
        causal_explanation=("PATH" if candidate.linked_symptoms else candidate.causal_explanation),
        linked_symptoms=tuple(sorted(candidate.linked_symptoms)),
    )


def _path_snapshot(hypothesis: Hypothesis) -> tuple[tuple[tuple[str, str, str], ...], ...]:
    return tuple(
        sorted(
            tuple((hop.source.canonical, hop.relation, hop.target.canonical) for hop in path)
            for path in hypothesis.causal_paths
        )
    )


def _hypothesis_snapshot(
    hypothesis: Hypothesis,
    *,
    plausible: bool,
    reasons: Sequence[str],
    leading: bool,
    verification_decision: str | None,
) -> HypothesisSnapshot:
    return HypothesisSnapshot(
        hypothesis_id=hypothesis.hypothesis_id,
        causal_actor=hypothesis.causal_actor.canonical,
        members=tuple(sorted(item.canonical for item in hypothesis.members)),
        manifestations=tuple(sorted(item.canonical for item in hypothesis.manifestations)),
        finding_keys=tuple(sorted(finding_key(item) for item in hypothesis.findings)),
        initiating_finding_keys=tuple(
            sorted(finding_key(item) for item in hypothesis.initiating_findings)
        ),
        supporting_finding_keys=tuple(
            sorted(finding_key(item) for item in hypothesis.supporting_findings)
        ),
        contradictory_finding_keys=tuple(
            sorted(finding_key(item) for item in hypothesis.contradictory_findings)
        ),
        causal_explanation=(
            "PATH" if any(hypothesis.causal_paths) else hypothesis.causal_explanation
        ),
        causal_paths=_path_snapshot(hypothesis),
        plausible=plausible,
        plausibility_reasons=tuple(sorted(reasons)),
        leading=leading,
        verification_decision=verification_decision,
    )


def _elimination_mechanics(
    hypothesis: Hypothesis, reason_codes: Sequence[str]
) -> EliminationMechanics:
    initiating = tuple(
        _finding_snapshot(item).finding_key for item in hypothesis.initiating_findings
    )
    contradictory = tuple(
        _finding_snapshot(item).finding_key for item in hypothesis.contradictory_findings
    )
    initiating_deltas = tuple(
        finding.onset_delta_seconds
        for finding in hypothesis.findings
        if finding.temporal_role is EvidenceTemporalRole.INITIATING
        and finding.onset_delta_seconds is not None
    )
    contradiction_deltas = tuple(
        finding.onset_delta_seconds
        for finding in hypothesis.contradictory_findings
        if finding.onset_delta_seconds is not None
    )
    return EliminationMechanics(
        hypothesis_id=hypothesis.hypothesis_id,
        causal_actor=hypothesis.causal_actor.canonical,
        reason_codes=tuple(sorted(set(reason_codes))),
        causal_explanation=hypothesis.causal_explanation,
        has_causal_symptom_link=hypothesis.causal_explanation in {"PATH", "DIRECT"},
        has_aligned_initiating_evidence=_has_aligned_initiating(hypothesis),
        initiating_findings=tuple(sorted(initiating)),
        initiating_deltas=tuple(sorted(initiating_deltas)),
        contradictory_findings=tuple(sorted(contradictory)),
        contradiction_deltas=tuple(sorted(contradiction_deltas)),
        evidence_ids=tuple(
            sorted({ref for item in hypothesis.findings for ref in item.evidence_ids})
        ),
    )


def build_blind_audit(
    source: ObservationSource,
    snapshot_entities: Sequence[SnapshotEntitySnapshot],
) -> CausalCompletenessBlindAudit:
    """Build all inventories before any ground-truth matching occurs."""
    case = build_case(source)
    diagnosis = diagnose_case(case)
    source_exposure = source_exposure_inventory(source)
    catalog_entities = {item.canonical for item in snapshot_entities}
    source_k8s_entities = set(source_exposure.history_entities) | set(
        source_exposure.event_entities
    )
    missing_from_catalog = tuple(sorted(source_k8s_entities - catalog_entities))
    catalog_consistency = CatalogConsistency(
        source_k8s_entity_count=len(source_k8s_entities),
        observable_catalog_count=len(catalog_entities),
        missing_from_catalog=missing_from_catalog,
        consistent=not missing_from_catalog,
    )
    if not catalog_consistency.consistent:
        raise ValueError(
            "SnapshotSource Kubernetes entities missing from observable catalog: "
            + ", ".join(missing_from_catalog)
        )
    trace = diagnosis.resolution_trace
    audit_by_id = {item.hypothesis_id: item for item in (trace.hypothesis_audits if trace else ())}
    plausible_ids = set(trace.plausible_hypotheses if trace else ())
    leading_ids = set(trace.leading_hypothesis_ids if trace else ())

    def verification_value(hypothesis_id: str) -> str | None:
        audit = audit_by_id.get(hypothesis_id)
        verification = audit.verification if audit else None
        decision = verification.decision if verification else None
        return decision.value if decision is not None else None

    findings = tuple(
        sorted(
            (_finding_snapshot(item) for item in case.findings), key=lambda item: item.finding_key
        )
    )
    hypotheses = tuple(
        _hypothesis_snapshot(
            item,
            plausible=item.hypothesis_id in plausible_ids,
            reasons=tuple(
                reason.value for reason in audit_by_id[item.hypothesis_id].plausibility_reasons
            )
            if item.hypothesis_id in audit_by_id
            else (),
            leading=item.hypothesis_id in leading_ids,
            verification_decision=verification_value(item.hypothesis_id),
        )
        for item in sorted(case.hypotheses, key=lambda item: item.hypothesis_id)
    )
    eliminations: list[EliminationMechanics] = []
    for hypothesis_id, audit in sorted(audit_by_id.items()):
        if audit.plausible:
            continue
        hypothesis = next(item for item in case.hypotheses if item.hypothesis_id == hypothesis_id)
        eliminations.append(
            _elimination_mechanics(
                hypothesis,
                tuple(reason.value for reason in audit.plausibility_reasons),
            )
        )
    selected_id = (
        trace.leading_hypothesis_ids[0] if trace and trace.leading_hypothesis_ids else None
    )
    selected_actor = next(
        (
            item.causal_actor.canonical
            for item in case.hypotheses
            if item.hypothesis_id == selected_id
        ),
        None,
    )
    return CausalCompletenessBlindAudit(
        incident_id=case.incident_id,
        snapshot_entities=tuple(sorted(snapshot_entities, key=lambda item: item.canonical)),
        source_exposure=source_exposure,
        catalog_consistency=catalog_consistency,
        findings=findings,
        candidates=tuple(
            _candidate_snapshot(item)
            for item in sorted(case.candidates, key=lambda item: item.entity.canonical)
        ),
        hypotheses=hypotheses,
        elimination_mechanics=tuple(eliminations),
        resolution=diagnosis.resolution.value,
        selected_hypothesis_id=selected_id,
        selected_actor=selected_actor,
    )


def forbidden_blind_keys(value: Any, path: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Return forbidden field paths found recursively in a blind payload."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).casefold()
            if key_text in _FORBIDDEN_BLIND_KEYS or key_text.startswith("gt_"):
                found.append(".".join((*path, str(key))))
            found.extend(forbidden_blind_keys(child, (*path, str(key))))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(forbidden_blind_keys(child, (*path, str(index))))
    return tuple(sorted(found))


def classify_first_loss(
    *,
    snapshot_matches: Sequence[str],
    source_matches: Sequence[str],
    direct_findings: Sequence[str],
    related_findings: Sequence[str],
    candidate_matches: Sequence[str],
    hypothesis_matches: Sequence[str],
    plausible_matches: Sequence[str],
    leading_matches: Sequence[str],
    resolution: str,
) -> str:
    """Apply the frozen first-loss order to already matched inventories."""
    if not snapshot_matches:
        return "SNAPSHOT_ENTITY_NOT_OBSERVABLE"
    if not source_matches:
        return "SOURCE_ADAPTER_ENTITY_GAP"
    if not direct_findings:
        return "FINDING_RELATED_ONLY" if related_findings else "FINDING_EXTRACTION_GAP"
    if not candidate_matches:
        return "CANDIDATE_CREATION_GAP"
    if not hypothesis_matches:
        return "HYPOTHESIS_GROUPING_GAP"
    if len(hypothesis_matches) > 1:
        return "MULTIPLE_MATCHING_HYPOTHESIS_EPISODES"
    if not plausible_matches:
        return "PLAUSIBILITY_ELIMINATION"
    if not leading_matches:
        return "PLAUSIBLE_NOT_LEADING"
    if resolution == Resolution.AMBIGUOUS.value:
        return "LEADING_AMBIGUOUS"
    return "UNIQUELY_RESOLVED"


def plausibility_diagnostic(reason_codes: Sequence[str]) -> str:
    values = set(reason_codes)
    categories = []
    if "NO_CAUSAL_SYMPTOM_LINK" in values:
        categories.append("LINKAGE_ELIMINATION")
    if "NO_ONSET_CAPABLE_INITIATING_EVIDENCE" in values:
        categories.append("INITIATING_EVIDENCE_ELIMINATION")
    if "EXPLICIT_TEMPORAL_CONTRADICTION" in values:
        categories.append("TEMPORAL_CONTRADICTION_ELIMINATION")
    if len(categories) > 1:
        return "MULTIPLE_PLAUSIBILITY_FAILURES"
    return categories[0] if categories else "UNCLASSIFIED_PLAUSIBILITY_FAILURE"


def extraction_diagnostic(
    snapshot: SnapshotEntitySnapshot | None, source: SourceExposureSnapshot
) -> str:
    """Classify a Finding-stage miss from observable records only."""
    if snapshot is None:
        return "UNCLASSIFIED_EXTRACTION_GAP"
    lifecycle_by_entity = dict(source.history_lifecycles)
    lifecycles = set(lifecycle_by_entity.get(snapshot.canonical, ()))
    history_counts = dict(source.history_counts)
    event_counts = dict(source.event_counts)
    effective_version_count = history_counts.get(snapshot.canonical, 0)
    effective_event_count = event_counts.get(snapshot.canonical, 0)
    if effective_version_count >= 2:
        return "MULTIPLE_OBJECT_VERSIONS_NO_FINDING"
    if "CREATED" in lifecycles:
        return "CREATED_OBJECT_NO_FINDING"
    if "DELETED" in lifecycles:
        return "DELETED_OBJECT_NO_FINDING"
    if "Chaos" in snapshot.kind or snapshot.kind.endswith("Chaos"):
        return "CHAOS_EVENT_NO_FINDING" if effective_event_count else "CHAOS_OBJECT_NO_FINDING"
    if effective_event_count:
        if any(item.type.casefold() == "warning" for item in snapshot.event_summaries):
            return "WARNING_EVENTS_NO_FINDING"
        return "OBSERVABLE_ENTITY_WITHOUT_SUPPORTED_SIGNAL_FAMILY"
    if (
        effective_version_count == 1
        and not effective_event_count
        and "CREATED" not in lifecycles
        and "DELETED" not in lifecycles
    ):
        return "STATIC_OBJECT_ONLY_NO_FINDING"
    return "UNCLASSIFIED_EXTRACTION_GAP"


def blind_audit_from_dict(value: Mapping[str, Any]) -> CausalCompletenessBlindAudit:
    """Decode only sealed blind data needed by the ground-truth overlay."""
    snapshots = tuple(
        SnapshotEntitySnapshot(
            **{
                **item,
                "object_versions": tuple(
                    ObjectObservationSnapshot(**entry) for entry in item["object_versions"]
                ),
                "event_summaries": tuple(
                    EventObservationSnapshot(**entry) for entry in item["event_summaries"]
                ),
            }
        )
        for item in value["snapshot_entities"]
    )
    source_value = value["source_exposure"]
    source = SourceExposureSnapshot(
        history_entities=tuple(source_value["history_entities"]),
        event_entities=tuple(source_value["event_entities"]),
        traffic_entities=tuple(source_value["traffic_entities"]),
        alert_services=tuple(source_value["alert_services"]),
        history_counts=tuple(tuple(item) for item in source_value["history_counts"]),
        history_lifecycles=tuple(
            (item[0], tuple(item[1])) for item in source_value.get("history_lifecycles", ())
        ),
        event_counts=tuple(tuple(item) for item in source_value["event_counts"]),
        traffic_counts=tuple(tuple(item) for item in source_value["traffic_counts"]),
    )
    consistency_value = value.get("catalog_consistency")
    if not isinstance(consistency_value, Mapping):
        catalog_entities = {item.canonical for item in snapshots}
        source_k8s_entities = set(source.history_entities) | set(source.event_entities)
        missing = tuple(sorted(source_k8s_entities - catalog_entities))
        consistency = CatalogConsistency(
            source_k8s_entity_count=len(source_k8s_entities),
            observable_catalog_count=len(catalog_entities),
            missing_from_catalog=missing,
            consistent=not missing,
        )
    else:
        consistency = CatalogConsistency(
            source_k8s_entity_count=int(consistency_value["source_k8s_entity_count"]),
            observable_catalog_count=int(consistency_value["observable_catalog_count"]),
            missing_from_catalog=tuple(consistency_value["missing_from_catalog"]),
            consistent=bool(consistency_value["consistent"]),
        )
    hypotheses = tuple(
        HypothesisSnapshot(
            **{
                **item,
                "members": tuple(item["members"]),
                "manifestations": tuple(item["manifestations"]),
                "finding_keys": tuple(item["finding_keys"]),
                "initiating_finding_keys": tuple(item["initiating_finding_keys"]),
                "supporting_finding_keys": tuple(item["supporting_finding_keys"]),
                "contradictory_finding_keys": tuple(item["contradictory_finding_keys"]),
                "causal_paths": tuple(
                    tuple(tuple(hop) for hop in path) for path in item["causal_paths"]
                ),
                "plausibility_reasons": tuple(item["plausibility_reasons"]),
            }
        )
        for item in value["hypotheses"]
    )
    return CausalCompletenessBlindAudit(
        incident_id=str(value["incident_id"]),
        snapshot_entities=snapshots,
        source_exposure=source,
        catalog_consistency=consistency,
        findings=tuple(FindingSnapshot(**item) for item in value["findings"]),
        candidates=tuple(CandidateSnapshot(**item) for item in value["candidates"]),
        hypotheses=hypotheses,
        elimination_mechanics=tuple(
            EliminationMechanics(**item) for item in value["elimination_mechanics"]
        ),
        resolution=str(value["resolution"]),
        selected_hypothesis_id=value.get("selected_hypothesis_id"),
        selected_actor=value.get("selected_actor"),
    )


__all__ = [
    "CandidateSnapshot",
    "CatalogConsistency",
    "CausalCompletenessBlindAudit",
    "EliminationMechanics",
    "EventObservationSnapshot",
    "FindingSnapshot",
    "HypothesisSnapshot",
    "ObjectObservationSnapshot",
    "RootCauseJourney",
    "ScenarioCompletenessOverlay",
    "SnapshotEntitySnapshot",
    "SourceExposureSnapshot",
    "build_blind_audit",
    "blind_audit_from_dict",
    "classify_first_loss",
    "extraction_diagnostic",
    "forbidden_blind_keys",
    "plausibility_diagnostic",
    "snapshot_entity_inventory",
    "source_exposure_inventory",
]
