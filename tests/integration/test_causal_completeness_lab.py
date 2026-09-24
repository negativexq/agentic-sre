"""Real-source controls for the P2C causal completeness funnel."""

from __future__ import annotations

from dataclasses import replace

from packages.evals.causal_completeness import (
    EventObservationSnapshot,
    ObjectObservationSnapshot,
    SnapshotEntitySnapshot,
    build_blind_audit,
    classify_first_loss,
    extraction_diagnostic,
)
from packages.rca.demo import demo_source
from packages.rca.engine import build_case, diagnose_case
from packages.rca.model import EntityRef, ObjectVersion
from packages.rca.source import InMemorySource


def _catalog(source: InMemorySource) -> tuple[SnapshotEntitySnapshot, ...]:
    entities = set(source.object_history()) | {event.entity for event in source.events()}
    snapshots = []
    for entity in sorted(entities, key=lambda item: item.canonical):
        versions = tuple(source.object_history().get(entity, ()))
        events = tuple(event for event in source.events() if event.entity == entity)
        snapshots.append(
            SnapshotEntitySnapshot(
                canonical=entity.canonical,
                namespace=entity.namespace,
                kind=entity.kind,
                name=entity.name,
                object_record_count=len(versions),
                event_record_count=len(events),
                topology_neighbors=(),
                topology_relations=(),
                object_versions=tuple(
                    ObjectObservationSnapshot(
                        evidence_id=item.evidence_id,
                        observed_at=item.observed_at.isoformat(),
                        lifecycle=item.lifecycle.value if item.lifecycle else None,
                        changed_from_previous=index > 0,
                    )
                    for index, item in enumerate(versions)
                ),
                event_summaries=tuple(
                    EventObservationSnapshot(
                        evidence_id=item.evidence_id,
                        reason=item.reason,
                        type=item.type,
                        first_at=item.first_at.isoformat() if item.first_at else None,
                        last_at=item.last_at.isoformat() if item.last_at else None,
                        count=item.count,
                    )
                    for item in events
                ),
            )
        )
    return tuple(snapshots)


def _change(
    source: InMemorySource, entity_text: str, minutes: int, evidence_id: str
) -> ObjectVersion:
    entity = EntityRef.parse(entity_text)
    original = next(
        item for item in source.versions if item.entity == entity and item.observed_at.minute == 0
    )
    body = dict(original.body)
    spec = dict(body.get("spec", {}))
    spec["replicas"] = int(spec.get("replicas", 1)) + 1
    body["spec"] = spec
    return original.model_copy(
        update={
            "observed_at": original.observed_at.replace(minute=minutes),
            "evidence_id": evidence_id,
            "body": body,
        }
    )


def test_unlinked_alternative_keeps_real_cause_in_leading_ambiguity() -> None:
    source = demo_source()
    audit = build_blind_audit(source, _catalog(source))
    case = build_case(source)
    diagnosis = diagnose_case(case)
    assert diagnosis.hypothesis is not None
    # The payment change is evidence-backed, but the unlinked recorder change
    # has no positive contradiction evidence. It must remain unresolved rather
    # than being removed from competition by missing linkage.
    assert audit.resolution == "AMBIGUOUS"
    assert any(item.entity == "shop/Deployment/payment" for item in audit.candidates)
    assert any(item.causal_actor == "shop/Deployment/payment" for item in audit.hypotheses)
    assert (
        classify_first_loss(
            snapshot_matches=("shop/Deployment/payment",),
            source_matches=("shop/Deployment/payment",),
            direct_findings=("payment-finding",),
            related_findings=(),
            candidate_matches=("shop/Deployment/payment",),
            hypothesis_matches=(diagnosis.hypothesis.hypothesis_id,),
            plausible_matches=(diagnosis.hypothesis.hypothesis_id,),
            leading_matches=(diagnosis.hypothesis.hypothesis_id,),
            resolution=diagnosis.resolution.value,
        )
        == "LEADING_AMBIGUOUS"
    )


def test_static_observable_cause_is_a_finding_stage_loss() -> None:
    source = demo_source()
    static = ObjectVersion(
        entity=EntityRef.parse("shop/ConfigMap/static-cause"),
        observed_at=source.versions[0].observed_at,
        body={
            "kind": "ConfigMap",
            "metadata": {"name": "static-cause", "namespace": "shop"},
            "data": {"mode": "stable"},
        },
        evidence_id="synthetic:static-cause",
    )
    source = replace(source, versions=[*source.versions, static])
    audit = build_blind_audit(source, _catalog(source))
    assert (
        classify_first_loss(
            snapshot_matches=("shop/ConfigMap/static-cause",),
            source_matches=("shop/ConfigMap/static-cause",),
            direct_findings=(),
            related_findings=(),
            candidate_matches=(),
            hypothesis_matches=(),
            plausible_matches=(),
            leading_matches=(),
            resolution=audit.resolution,
        )
        == "FINDING_EXTRACTION_GAP"
    )
    snapshot = next(
        item for item in audit.snapshot_entities if item.canonical == "shop/ConfigMap/static-cause"
    )
    assert extraction_diagnostic(snapshot, audit.source_exposure) == "STATIC_OBJECT_ONLY_NO_FINDING"


def test_two_independent_changes_remain_a_real_leading_ambiguity() -> None:
    source = demo_source()
    first = _change(source, "shop/Deployment/payment", 7, "synthetic:payment")
    second = _change(source, "shop/Deployment/cart", 7, "synthetic:cart")
    source = replace(
        source, error_items=[], event_items=[], versions=[*source.versions, first, second]
    )
    audit = build_blind_audit(source, _catalog(source))
    assert audit.resolution == "AMBIGUOUS"
    assert sum(item.leading for item in audit.hypotheses) >= 2
