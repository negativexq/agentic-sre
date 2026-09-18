"""Production-path temporal provenance controls for P2D-0."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from packages.evals.temporal_plausibility import (
    TemporalPlausibilityBlindAudit,
    build_temporal_audit,
)
from packages.rca.demo import demo_source
from packages.rca.engine import build_case, diagnose_case


def _audit_with_payment_change(
    previous_minutes: int, current_minutes: int
) -> TemporalPlausibilityBlindAudit:
    source = demo_source()
    original = next(
        item
        for item in source.versions
        if item.entity.canonical == "shop/Deployment/payment" and item.observed_at.minute == 0
    )
    body = dict(original.body)
    spec = dict(body["spec"])
    spec["replicas"] = int(spec.get("replicas", 1)) + 1
    body["spec"] = spec
    changed = original.model_copy(
        update={
            "observed_at": original.observed_at + timedelta(minutes=current_minutes),
            "evidence_id": f"synthetic:payment:{current_minutes}",
            "body": body,
        }
    )
    versions = [
        item
        for item in source.versions
        if not (
            item.entity.canonical == "shop/Deployment/payment" and item.observed_at.minute in {0, 7}
        )
    ]
    if previous_minutes:
        previous = original.model_copy(
            update={
                "observed_at": original.observed_at + timedelta(minutes=previous_minutes),
                "evidence_id": "synthetic:payment:before",
            }
        )
        versions.extend((previous, changed))
    else:
        versions.extend((original, changed))
    source = replace(source, versions=versions, error_items=[], event_items=[])
    case = build_case(source)
    return build_temporal_audit(case, diagnose_case(case))


def test_real_production_path_captures_early_object_change() -> None:
    audit = _audit_with_payment_change(0, 7)
    assert any(
        item.kind in {"SPEC_CHANGE", "SCALE_CHANGE"}
        and item.interval_relation_to_onset_grace == "DEFINITELY_ONSET_CAPABLE"
        for item in audit.temporal_evidence
    )


def test_real_production_path_captures_definitely_late_object_change() -> None:
    audit = _audit_with_payment_change(26, 30)
    assert any(
        item.kind in {"SPEC_CHANGE", "SCALE_CHANGE"}
        and item.interval_relation_to_onset_grace == "DEFINITELY_LATE"
        for item in audit.temporal_evidence
    )


def test_real_production_path_captures_straddling_object_interval() -> None:
    audit = _audit_with_payment_change(10, 30)
    assert any(
        item.kind in {"SPEC_CHANGE", "SCALE_CHANGE"}
        and item.interval_relation_to_onset_grace == "STRADDLES_ONSET_GRACE"
        for item in audit.temporal_evidence
    )
