"""Production-path synthetic controls for the P2A diagnostic lab."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import pytest

from packages.evals.causal_semantics import audit_source
from packages.rca.demo import demo_source
from packages.rca.model import EntityRef, ObjectVersion
from packages.rca.source import InMemorySource


def _change_version(
    source: InMemorySource, entity_text: str, evidence_id: str, minutes: int
) -> ObjectVersion:
    entity = EntityRef.parse(entity_text)
    versions = source.versions
    original = next(
        item for item in versions if item.entity == entity and item.observed_at.minute == 0
    )
    body = dict(original.body)
    spec = dict(body.get("spec", {}))
    spec["replicas"] = int(spec.get("replicas", 1)) + 1
    body["spec"] = spec
    return original.model_copy(
        update={
            "observed_at": original.observed_at + timedelta(minutes=minutes),
            "evidence_id": evidence_id,
            "body": body,
        }
    )


def _single_change_source() -> InMemorySource:
    source = demo_source()
    return replace(source, error_items=[])


@pytest.mark.parametrize(
    ("name", "source_builder"),
    (
        ("single_causal_change", _single_change_source),
        ("two_independent_changes", lambda: _two_change_source()),
        ("actor_and_manifestation", demo_source),
        ("late_competing_change", lambda: _late_change_source()),
    ),
)
def test_synthetic_controls_use_real_production_path(
    name: str, source_builder: Callable[[], InMemorySource]
) -> None:
    source = source_builder()
    audit = audit_source(source)
    assert audit.finding_count >= 0
    assert audit.stage_funnel["findings"] == audit.finding_count
    assert audit.stage_funnel["hypotheses"] == audit.hypothesis_count
    if name == "two_independent_changes":
        assert audit.resolution == "AMBIGUOUS"
        supported_leading = {
            item.causal_actor
            for item in audit.hypothesis_snapshots
            if item.plausible and item.is_leading
        }
        assert supported_leading == {
            "shop/Deployment/cart",
            "shop/Deployment/payment",
        }
        unresolved_unlinked = next(
            item
            for item in audit.hypothesis_snapshots
            if item.causal_actor == "observability/ConfigMap/recorder"
        )
        assert unresolved_unlinked.causal_explanation == "UNLINKED"
        assert not unresolved_unlinked.plausible
        assert unresolved_unlinked.is_leading
    if name == "late_competing_change":
        assert audit.resolution == "AMBIGUOUS"
        assert any(
            pair.both_leading and "EXACT_TEMPORAL_ORDER_AVAILABLE" in pair.diagnostic_signals
            for pair in audit.competitions
        )


def _two_change_source() -> InMemorySource:
    source = demo_source()
    first = _change_version(source, "shop/Deployment/payment", "synthetic:payment-change", 7)
    second = _change_version(source, "shop/Deployment/cart", "synthetic:cart-change", 7)
    return replace(
        source,
        error_items=[],
        event_items=[],
        versions=[*source.versions, first, second],
    )


def _late_change_source() -> InMemorySource:
    source = demo_source()
    late = _change_version(source, "shop/Deployment/cart", "synthetic:late-change", 20)
    return replace(
        source,
        error_items=[],
        event_items=[],
        versions=[*source.versions, late],
    )
