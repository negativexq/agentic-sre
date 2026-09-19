from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.rca.engine import build_case
from packages.rca.investigation.environment import initial_view
from packages.rca.investigation.evidence import (
    InMemoryEvidenceStore,
    OverlayObservationSource,
    records_from_observation,
    visible_evidence_refs,
)
from packages.rca.investigation.graph import (
    _check_novelty,
    _check_progress,
    _Runtime,
    build_investigation_state,
    world_model_fingerprint,
)
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.investigation.state import InvestigationConfig
from packages.rca.model import (
    ClusterEvent,
    EntityRef,
    FrontierStatus,
    InvestigationObservation,
    Lifecycle,
    ObjectVersion,
    StructuralAlternative,
    TraceSpanObservation,
    TraceSpanStatus,
)
from packages.rca.source import InMemorySource

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _entity(kind: str, name: str) -> EntityRef:
    return EntityRef(namespace="shop", kind=kind, name=name)


def _version(ref: str, at: datetime, body: dict[str, Any], lifecycle: Lifecycle) -> ObjectVersion:
    return ObjectVersion(
        entity=EntityRef.parse(ref),
        observed_at=at,
        body=body,
        evidence_id=f"object:{ref}:{at.isoformat()}",
        lifecycle=lifecycle,
    )


def _span(
    *, trace_id: str, span_id: str, service: str, kind: str, parent: str | None = None
) -> TraceSpanObservation:
    return TraceSpanObservation(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        service=service,
        span_kind=kind,
        start_at=T0,
        end_at=T0 + timedelta(seconds=1),
        status=TraceSpanStatus.ERROR if kind == "SERVER" else TraceSpanStatus.UNSET,
        semantic_attributes={
            "k8s.namespace.name": "shop",
            "k8s.deployment.name": service,
            "k8s.pod.name": f"{service}-pod",
            "http.response.status_code": "500" if kind == "SERVER" else "200",
        },
        evidence_id=f"trace:{trace_id}:{span_id}",
    )


def test_store_idempotency_and_collision() -> None:
    record = _version("shop/ConfigMap/foo", T0, {"metadata": {"name": "foo"}}, Lifecycle.CREATED)
    store = InMemoryEvidenceStore()
    assert store.put(record)
    assert not store.put(record)
    assert store.refs() == (record.evidence_id,)
    with pytest.raises(ValueError):
        store.put(record.model_copy(update={"body": {"metadata": {"name": "other"}}}))
    with pytest.raises(ValueError):
        store.put(
            ClusterEvent(
                entity=record.entity,
                reason="Created",
                evidence_id=record.evidence_id,
            )
        )


def test_history_merge_and_engine_derived_finding() -> None:
    entity = _entity("ConfigMap", "foo")
    old = _version(
        entity.canonical,
        T0,
        {"metadata": {"name": "foo"}, "data": {"key": "old"}},
        Lifecycle.CREATED,
    )
    current = _version(
        entity.canonical,
        T0 + timedelta(minutes=1),
        {"metadata": {"name": "foo"}, "data": {"key": "new"}},
        Lifecycle.UPDATED,
    )
    base = initial_view(InMemorySource(name="history", versions=[old, current]))
    store = InMemoryEvidenceStore()
    store.put(old)
    overlay = OverlayObservationSource(base, store, (old.evidence_id,), frozenset({"history"}))
    assert tuple(item.evidence_id for item in overlay.object_history()[entity]) == (
        old.evidence_id,
        current.evidence_id,
    )
    case = build_case(overlay)
    assert any(
        finding.entity == entity
        and old.evidence_id in finding.evidence_ids
        and current.evidence_id in finding.evidence_ids
        for finding in case.findings
    )
    assert (
        sum(
            finding.entity == entity
            and old.evidence_id in finding.evidence_ids
            and current.evidence_id in finding.evidence_ids
            for finding in case.findings
        )
        == 1
    )


def test_native_observation_records_and_malformed_payload() -> None:
    event = ClusterEvent(entity=_entity("Pod", "p"), reason="Started", evidence_id="event:1")
    observation = InvestigationObservation(
        observation_id="observation:1",
        gap_id="gap:1",
        capability="events",
        target=event.entity,
        payload={"events": [event.model_dump(mode="json")]},
        evidence_refs=(event.evidence_id,),
    )
    assert records_from_observation(observation) == (event,)
    malformed = observation.model_copy(update={"payload": {"events": [{"bad": True}]}})
    with pytest.raises(ValueError):
        records_from_observation(malformed)


def test_overlay_visibility_and_hidden_source_leakage() -> None:
    spans = [
        _span(trace_id="t", span_id=name, service=name, kind="SERVER") for name in ("a", "b", "c")
    ]
    source = InMemorySource(name="traces", trace_items=spans)
    base = initial_view(source)
    assert base.trace_observations() == ()
    store = InMemoryEvidenceStore()
    store.put(spans[1])
    overlay = OverlayObservationSource(base, store, (spans[1].evidence_id,), frozenset())
    assert tuple(item.evidence_id for item in overlay.trace_observations()) == (
        spans[1].evidence_id,
    )


def test_acquired_trace_rebuilds_existing_runtime_products() -> None:
    root = _span(trace_id="t", span_id="root", service="frontend", kind="CLIENT")
    child = _span(trace_id="t", span_id="child", service="backend", kind="SERVER", parent="root")
    source = InMemorySource(name="runtime", trace_items=[root, child])
    base = initial_view(source)
    empty_case = build_case(base)
    store = InMemoryEvidenceStore()
    store.put_many((root, child))
    overlay = OverlayObservationSource(
        base,
        store,
        (root.evidence_id, child.evidence_id),
        frozenset(),
    )
    acquired_case = build_case(overlay)
    assert not empty_case.runtime_graph.edges
    assert acquired_case.runtime_graph.edges
    assert acquired_case.runtime_evidence.service_outcomes
    assert world_model_fingerprint(empty_case) != world_model_fingerprint(acquired_case)


def test_frontier_bookkeeping_does_not_change_world_model_fingerprint() -> None:
    case = build_case(initial_view(InMemorySource(name="frontier")))
    alternatives = case.structural_alternatives or [
        StructuralAlternative(
            alternative_id="alternative:test",
            actor=_entity("ConfigMap", "foo"),
            role="configuration",
        )
    ]
    changed = replace(
        case,
        structural_alternatives=[
            alternative.model_copy(update={"status": FrontierStatus.PROMOTED})
            for alternative in alternatives
        ],
    )
    assert world_model_fingerprint(case) == world_model_fingerprint(changed)


def test_visible_refs_are_raw_source_refs_not_finding_refs() -> None:
    event = ClusterEvent(entity=_entity("Pod", "p"), reason="Started", evidence_id="event:visible")
    source = InMemorySource(name="visible", event_items=[event])
    assert event.evidence_id in visible_evidence_refs(source)


def test_base_visible_native_record_is_already_known() -> None:
    event = ClusterEvent(entity=_entity("Pod", "p"), reason="Started", evidence_id="event:known")
    source = InMemorySource(name="known", event_items=[event])
    case = build_case(source)
    state = build_investigation_state(source, initial_case=case)
    state["pending_observation"] = InvestigationObservation(
        observation_id="observation:known",
        gap_id="gap:known",
        capability="events",
        target=event.entity,
        payload={"events": [event.model_dump(mode="json")]},
        evidence_refs=(event.evidence_id,),
    )
    runtime = _Runtime(
        source=source,
        policy=ScriptedInvestigationPolicy([]),
        tools={},
        config=InvestigationConfig(),
        initial_case=case,
        rebuild_case=None,
        backend=None,
        evidence_store=None,
    )
    result = _check_novelty(state, runtime)
    assert result["pending_new_evidence_refs"] == ()
    assert result["pending_already_known_refs"] == (event.evidence_id,)
    assert result["acquired_evidence_refs"] == ()


def test_raw_evidence_progress_does_not_increment_no_progress() -> None:
    source = InMemorySource(name="progress")
    case = build_case(source)
    state = build_investigation_state(source, initial_case=case)
    state["last_new_raw_evidence_count"] = 1
    runtime = _Runtime(
        source=source,
        policy=ScriptedInvestigationPolicy([]),
        tools={},
        config=InvestigationConfig(max_no_progress_rounds=2),
        initial_case=case,
        rebuild_case=None,
        backend=None,
        evidence_store=None,
    )
    result = _check_progress(state, runtime)
    assert result["no_progress_count"] == 0


def test_true_no_progress_still_increments() -> None:
    source = InMemorySource(name="no-progress")
    case = build_case(source)
    state = build_investigation_state(source, initial_case=case)
    runtime = _Runtime(
        source=source,
        policy=ScriptedInvestigationPolicy([]),
        tools={},
        config=InvestigationConfig(max_no_progress_rounds=2),
        initial_case=case,
        rebuild_case=None,
        backend=None,
        evidence_store=None,
    )
    result = _check_progress(state, runtime)
    assert result["no_progress_count"] == 1
