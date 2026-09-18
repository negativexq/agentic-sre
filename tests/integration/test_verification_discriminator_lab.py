"""Production-path safety controls for the P2B-0 counterfactuals."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from packages.evals.verification_discriminator import VerificationDiscriminatorAudit, audit_case
from packages.rca.demo import START, _deployment, demo_source
from packages.rca.engine import build_case, diagnose_case
from packages.rca.model import EntityRef, Lifecycle, ObjectVersion
from packages.rca.source import InMemorySource


def _visible_verified_and_likely_source() -> InMemorySource:
    source = demo_source()
    entity = EntityRef.parse("shop/Deployment/rogue")
    version = ObjectVersion(
        entity=entity,
        observed_at=START + timedelta(minutes=7),
        body={
            "kind": "Deployment",
            "metadata": {
                "name": "rogue",
                "namespace": "shop",
                "creationTimestamp": (START + timedelta(minutes=7)).isoformat(),
                "labels": {"app": "checkout"},
            },
            "spec": _deployment("rogue", {})["spec"],
        },
        evidence_id="synthetic:rogue-created",
        lifecycle=Lifecycle.CREATED,
    )
    return replace(source, error_items=[], versions=[*source.versions, version])


def _change_version(
    source: InMemorySource, entity_text: str, evidence_id: str, minutes: int
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
            "observed_at": original.observed_at + timedelta(minutes=minutes),
            "evidence_id": evidence_id,
            "body": body,
        }
    )


def _two_change_source() -> InMemorySource:
    source = demo_source()
    first = _change_version(source, "shop/Deployment/payment", "synthetic:payment-change", 7)
    second = _change_version(source, "shop/Deployment/cart", "synthetic:cart-change", 7)
    return replace(
        source, error_items=[], event_items=[], versions=[*source.versions, first, second]
    )


def _late_change_source() -> InMemorySource:
    source = demo_source()
    late = _change_version(source, "shop/Deployment/cart", "synthetic:late-change", 20)
    return replace(source, error_items=[], event_items=[], versions=[*source.versions, late])


def _audit(source: InMemorySource) -> VerificationDiscriminatorAudit:
    case = build_case(source)
    return audit_case(case, diagnose_case(case))


def test_unique_verified_cause_runs_through_production_path() -> None:
    audit = _audit(_visible_verified_and_likely_source())
    results = {item.rule_id: item for item in audit.rule_results}
    assert audit.current_resolution == "AMBIGUOUS"
    assert results["V1_UNIQUE_VERIFIED"].triggered
    assert results["V1_UNIQUE_VERIFIED"].selected_actor == "shop/Deployment/payment"


def test_two_verified_production_causes_abstain() -> None:
    audit = _audit(_two_change_source())
    assert audit.current_resolution == "AMBIGUOUS"
    assert all(not item.triggered for item in audit.rule_results)
    assert all(
        item.abstention_reason == "MULTIPLE_VERIFIED_HYPOTHESES" for item in audit.rule_results
    )


def test_late_competitor_reports_actual_production_predicates() -> None:
    audit = _audit(_late_change_source())
    assert audit.current_resolution == "AMBIGUOUS"
    assert all(item.decision == "VERIFIED" for item in audit.hypothesis_verifications)
    assert all(not item.triggered for item in audit.rule_results)


def test_hidden_cause_safety_control_exposes_v1_false_confidence() -> None:
    audit = _audit(_visible_verified_and_likely_source())
    hidden_entity = "shop/Deployment/hidden-cause"
    represented = {item.causal_actor for item in audit.hypothesis_verifications}
    assert hidden_entity not in represented
    v1 = next(item for item in audit.rule_results if item.rule_id == "V1_UNIQUE_VERIFIED")
    assert v1.triggered
    # The external fixture construction knows the omitted cause; production
    # logic receives no such expected entity.  This is intentionally unsafe.
    assert v1.selected_actor != hidden_entity
