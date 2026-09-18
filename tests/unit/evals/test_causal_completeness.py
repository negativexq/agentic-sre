"""Unit coverage for the P2C.1 causal-completeness inventory."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from packages.evals.causal_completeness import (
    _object_snapshot,
    classify_first_loss,
    extraction_diagnostic,
    forbidden_blind_keys,
    plausibility_diagnostic,
)
from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.snapshot_backend import _record_entity


def _stage(**overrides: object) -> str:
    values: dict[str, object] = {
        "snapshot_matches": ("ns/Deployment/a",),
        "source_matches": ("ns/Deployment/a",),
        "direct_findings": ("finding",),
        "related_findings": (),
        "candidate_matches": ("ns/Deployment/a",),
        "hypothesis_matches": ("h:a",),
        "plausible_matches": ("h:a",),
        "leading_matches": ("h:a",),
        "resolution": "RESOLVED",
    }
    values.update(overrides)
    return classify_first_loss(**values)  # type: ignore[arg-type]


def test_first_loss_classifier_covers_every_stage() -> None:
    assert _stage(snapshot_matches=()) == "SNAPSHOT_ENTITY_NOT_OBSERVABLE"
    assert _stage(source_matches=()) == "SOURCE_ADAPTER_ENTITY_GAP"
    assert _stage(direct_findings=()) == "FINDING_EXTRACTION_GAP"
    assert _stage(direct_findings=(), related_findings=("related",)) == "FINDING_RELATED_ONLY"
    assert _stage(candidate_matches=()) == "CANDIDATE_CREATION_GAP"
    assert _stage(hypothesis_matches=()) == "HYPOTHESIS_GROUPING_GAP"
    assert _stage(hypothesis_matches=("h:a", "h:b")) == "MULTIPLE_MATCHING_HYPOTHESIS_EPISODES"
    assert _stage(plausible_matches=()) == "PLAUSIBILITY_ELIMINATION"
    assert _stage(leading_matches=()) == "PLAUSIBLE_NOT_LEADING"
    assert _stage(resolution="AMBIGUOUS") == "LEADING_AMBIGUOUS"
    assert _stage() == "UNIQUELY_RESOLVED"


def test_plausibility_diagnostic_preserves_multiple_reasons() -> None:
    assert plausibility_diagnostic(("NO_CAUSAL_SYMPTOM_LINK",)) == "LINKAGE_ELIMINATION"
    assert (
        plausibility_diagnostic(("NO_ONSET_CAPABLE_INITIATING_EVIDENCE",))
        == "INITIATING_EVIDENCE_ELIMINATION"
    )
    assert (
        plausibility_diagnostic(("EXPLICIT_TEMPORAL_CONTRADICTION",))
        == "TEMPORAL_CONTRADICTION_ELIMINATION"
    )
    assert (
        plausibility_diagnostic(("NO_CAUSAL_SYMPTOM_LINK", "EXPLICIT_TEMPORAL_CONTRADICTION"))
        == "MULTIPLE_PLAUSIBILITY_FAILURES"
    )


def test_forbidden_blind_fields_are_recursive() -> None:
    assert forbidden_blind_keys({"nested": {"ground_truth": False}}) == ("nested.ground_truth",)
    assert forbidden_blind_keys({"resolution": "AMBIGUOUS"}) == ()


def test_first_loss_invariants_are_monotonic_by_stage() -> None:
    assert _stage(direct_findings=(), candidate_matches=("ignored",)) == "FINDING_EXTRACTION_GAP"
    assert _stage(hypothesis_matches=(), plausible_matches=("h:a",)) == "HYPOTHESIS_GROUPING_GAP"


def _record(body: dict[str, object]) -> dict[str, object]:
    return {"record": {"Body": json.dumps(body)}}


def test_record_entity_unwraps_wrapped_event_involved_object() -> None:
    item = _record(
        {
            "object": {
                "kind": "Event",
                "metadata": {"name": "event-a", "namespace": "chaos-mesh"},
                "involvedObject": {
                    "kind": "NetworkChaos",
                    "namespace": "chaos-mesh",
                    "name": "chaos-a",
                },
            }
        }
    )
    entity = _record_entity(item)
    assert entity is not None
    assert entity.canonical == "chaos-mesh/NetworkChaos/chaos-a"


def test_record_entity_supports_unwrapped_event_and_ordinary_object() -> None:
    event = _record(
        {
            "kind": "Event",
            "metadata": {"name": "event-a", "namespace": "shop"},
            "involvedObject": {"kind": "Deployment", "namespace": "shop", "name": "checkout"},
        }
    )
    ordinary = _record(
        {"kind": "Deployment", "metadata": {"name": "checkout", "namespace": "shop"}}
    )
    assert _record_entity(event).canonical == "shop/Deployment/checkout"  # type: ignore[union-attr]
    assert _record_entity(ordinary).canonical == "shop/Deployment/checkout"  # type: ignore[union-attr]


class _StructuredBackend:
    def __init__(
        self,
        objects: tuple[dict[str, object], ...],
        events: tuple[dict[str, object], ...],
        edges: tuple[dict[str, str], ...] = (),
    ) -> None:
        self.objects = objects
        self.events = events
        self.edges = edges

    def observable_entities(self) -> tuple[dict[str, str], ...]:
        values = {}
        for item in (*self.objects, *self.events):
            entity = _record_entity(item)
            if entity is not None:
                values[entity.canonical] = {
                    "namespace": entity.namespace or "_cluster",
                    "kind": entity.kind,
                    "name": entity.name,
                }
        return tuple(values[key] for key in sorted(values))

    def complete_source_records(
        self, category: ITBenchEvidenceCategory
    ) -> Iterator[dict[str, object]]:
        return iter(
            self.objects if category is ITBenchEvidenceCategory.K8S_OBJECTS else self.events
        )

    def topology(self, *, limit: int | None = None) -> Iterator[dict[str, str]]:
        return iter(self.edges if limit is None else self.edges[:limit])


def test_structured_source_entities_are_subset_of_catalog_and_topology_is_local() -> None:
    objects = (
        _record({"kind": "Deployment", "metadata": {"name": "a", "namespace": "shop"}}),
        _record({"kind": "Deployment", "metadata": {"name": "c", "namespace": "shop"}}),
    )
    events = (
        _record(
            {
                "object": {
                    "kind": "Event",
                    "metadata": {"name": "event-a", "namespace": "shop"},
                    "involvedObject": {
                        "kind": "NetworkChaos",
                        "namespace": "chaos-mesh",
                        "name": "chaos-a",
                    },
                }
            }
        ),
    )
    backend = _StructuredBackend(
        objects,
        events,
        (
            {"source": "shop/Deployment/a", "relationship": "owns", "target": "shop/Deployment/b"},
            {"source": "shop/Deployment/c", "relationship": "owns", "target": "shop/Deployment/d"},
        ),
    )
    from packages.evals.causal_completeness import snapshot_entity_inventory

    snapshots = snapshot_entity_inventory(backend)
    catalog = {item.canonical for item in snapshots}
    source_k8s_entities = {
        "shop/Deployment/a",
        "shop/Deployment/c",
        "chaos-mesh/NetworkChaos/chaos-a",
    }
    assert source_k8s_entities <= catalog
    by_entity = {item.canonical: item for item in snapshots}
    assert by_entity["shop/Deployment/a"].topology_relations == (
        "shop/Deployment/a|owns|shop/Deployment/b",
    )
    assert by_entity["shop/Deployment/c"].topology_relations == (
        "shop/Deployment/c|owns|shop/Deployment/d",
    )


def test_effective_source_history_controls_extraction_diagnostic() -> None:
    from packages.evals.causal_completeness import SnapshotEntitySnapshot, SourceExposureSnapshot

    snapshot = SnapshotEntitySnapshot(
        canonical="shop/ConfigMap/static",
        namespace="shop",
        kind="ConfigMap",
        name="static",
        object_record_count=2,
        event_record_count=0,
        topology_neighbors=(),
        topology_relations=(),
        object_versions=(),
        event_summaries=(),
    )
    static_source = SourceExposureSnapshot(
        history_entities=("shop/ConfigMap/static",),
        event_entities=(),
        traffic_entities=(),
        alert_services=(),
        history_counts=(("shop/ConfigMap/static", 1),),
        history_lifecycles=(("shop/ConfigMap/static", ("OBSERVED",)),),
        event_counts=(),
        traffic_counts=(),
    )
    multi_source = static_source.__class__(
        **{**static_source.__dict__, "history_counts": (("shop/ConfigMap/static", 2),)}
    )
    assert extraction_diagnostic(snapshot, static_source) == "STATIC_OBJECT_ONLY_NO_FINDING"
    assert extraction_diagnostic(snapshot, multi_source) == "MULTIPLE_OBJECT_VERSIONS_NO_FINDING"


def test_raw_snapshot_lifecycle_is_not_used_as_production_lifecycle() -> None:
    snapshot = _object_snapshot(
        {
            "evidence_id": "raw:object",
            "Timestamp": "2026-01-01T00:00:00Z",
            "record": {
                "Body": json.dumps(
                    {
                        "kind": "Deployment",
                        "metadata": {
                            "name": "checkout",
                            "namespace": "shop",
                            "lifecycle": "CREATED",
                        },
                    }
                )
            },
        },
        None,
    )
    assert snapshot.lifecycle is None


def test_blind_audit_fails_closed_on_catalog_source_mismatch() -> None:
    from packages.evals.causal_completeness import build_blind_audit
    from packages.rca.demo import demo_source

    with pytest.raises(ValueError, match="missing from observable catalog"):
        build_blind_audit(demo_source(), ())
