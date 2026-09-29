"""M19-7.P2: T5/T6 replay ablations on A1 and A2 fixtures — eval-only, product unchanged."""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

import pytest

from packages.evals.product.ablation import (
    ABLATION_AMBIGUOUS,
    FAIL,
    PASS,
    EvalRuleOverrides,
    eval_config_digest,
    evidence_ablation,
    rule_ablation,
)
from packages.rca.demo import _at, _deployment, _version, _workload
from packages.rca.engine import EngineConfig, build_case, diagnose_case
from packages.rca.episode_end import RULE_ID as A1_ID
from packages.rca.episode_end import RULE_VERSION as A1_VERSION
from packages.rca.investigation.state import rca_config_digest
from packages.rca.model import (
    Alert,
    ClusterEvent,
    Diagnosis,
    EntityRef,
    Resolution,
    ResolutionElimination,
    ResourcePressure,
)
from packages.rca.resource_mechanism import RULE_ID as A2_ID
from packages.rca.resource_mechanism import RULE_VERSION as A2_VERSION
from packages.rca.source import InMemorySource

ROOT = Path(__file__).resolve().parents[3]
A1 = (A1_ID, A1_VERSION)
A2 = (A2_ID, A2_VERSION)
POD_B = "shop/Pod/checkout-6c9d8f7b5-k2x9p"
ALERT = Alert(
    name="HighRequestLatency",
    service="checkout",
    namespace="shop",
    starts_at=_at(10),
    labels={"alertname": "HighRequestLatency", "service_name": "checkout", "namespace": "shop"},
)
PAYMENT_ROOT = _version(
    "shop/Deployment/payment",
    7,
    _deployment("payment", {"PAYMENT_TIMEOUT_MS": "800", "FAULT_DELAY_MS": "2500"}),
)


def _pod_b(minutes: float, ready_since: float, ready: str = "True") -> Any:
    since = _at(ready_since).isoformat().replace("+00:00", "Z")
    return _version(
        POD_B,
        minutes,
        {
            "metadata": {
                "uid": "u-b",
                "labels": {"app": "checkout"},
                "ownerReferences": [{"kind": "ReplicaSet", "name": "checkout-6c9d8f7b5"}],
            },
            "status": {
                "conditions": [{"type": "Ready", "status": ready, "lastTransitionTime": since}],
                "containerStatuses": [{"name": "app", "restartCount": 0}],
            },
        },
    )


def a1_source() -> InMemorySource:
    """PR-01's H_B shape: a pre-onset readiness loss on Pod u-b, Ready again before onset
    and still Ready past onset + grace; the payment change is the true root."""
    versions = [
        *[
            item
            for item in _workload("checkout", 0, {"PAYMENT_ADDR": "payment:8080"})
            if item.entity.kind != "Pod"
        ],
        *_workload("payment", 0, {"PAYMENT_TIMEOUT_MS": "800", "FAULT_DELAY_MS": "0"}),
        PAYMENT_ROOT,
        _pod_b(0, -30),
        _pod_b(1, 1, "False"),
        _pod_b(2, 2),
        _pod_b(26, 2),  # observed >= onset + grace: the RECOVERED evidence
    ]
    event = ClusterEvent(
        entity=EntityRef.parse(POD_B),
        involved_uid="u-b",
        reason="Unhealthy",
        type="Warning",
        message="Readiness probe failed: HTTP probe failed with statuscode: 503",
        first_at=_at(1),
        last_at=_at(1.5),
        count=3,
        evidence_id="event:b-unhealthy",
    )
    return InMemorySource(
        name="a1",
        alert_coverage_start=_at(0),
        alert_items=[ALERT],
        versions=versions,
        event_items=[event],
        cutoff=_at(27),
    )


def _cart(memory: str) -> dict[str, Any]:
    body = _deployment("cart", {})
    body["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"memory": memory}}
    return body


def a2_source() -> InMemorySource:
    """PR-03's H_mem shape: a resource-only memory-limit reduction on cart with normal,
    fully covered working set; the payment change is the true root."""
    replica_set = {
        "metadata": {
            "ownerReferences": [{"kind": "Deployment", "name": "cart", "controller": True}]
        },
        "spec": copy.deepcopy(_cart("128Mi")["spec"]),
    }
    pod = {
        "metadata": {
            "uid": "u-cart",
            "labels": {"app": "cart"},
            "ownerReferences": [{"kind": "ReplicaSet", "name": "cart-new", "controller": True}],
            "creationTimestamp": _at(8.1).isoformat(),
        }
    }
    versions = [
        *_workload("checkout", 0, {"PAYMENT_ADDR": "payment:8080", "CART_ADDR": "cart:8080"}),
        *_workload("payment", 0, {"PAYMENT_TIMEOUT_MS": "800", "FAULT_DELAY_MS": "0"}),
        PAYMENT_ROOT,
        _version("shop/Deployment/cart", 0, _cart("512Mi")),
        _version("shop/Service/cart", 0, {"spec": {"selector": {"app": "cart"}}}),
        _version("shop/Deployment/cart", 8, _cart("128Mi")),
        _version("shop/ReplicaSet/cart-new", 8, replica_set),
        _version("shop/Pod/cart-new-a", 8.1, pod),
    ]
    coverage = ResourcePressure(
        pod=EntityRef.parse("shop/Pod/cart-new-a"),
        container="cart",
        resource="memory",
        baseline=0.30,
        peak=0.31,
        at=_at(10),
        evidence_id="prometheus:resource:cart",
        sample_count=120,
        sample_start=_at(0),
        sample_end=_at(30),
    )
    return InMemorySource(
        name="a2",
        alert_coverage_start=_at(0),
        alert_items=[ALERT],
        versions=versions,
        pressure_items=[coverage],
        cutoff=_at(31),
    )


def _full(source: InMemorySource) -> Diagnosis:
    config = EngineConfig()
    return diagnose_case(build_case(source, config), config=config)


def _target(diagnosis: Diagnosis, rule: tuple[str, str]) -> tuple[str, ResolutionElimination]:
    assert diagnosis.resolution_trace is not None
    (elimination,) = [
        item
        for item in diagnosis.resolution_trace.eliminations
        if (item.rule_id, item.rule_version) == rule
    ]
    (key,) = [
        item.hypothesis_key
        for item in diagnosis.hypothesis_inventory
        if item.hypothesis_id == elimination.hypothesis_id
    ]
    assert key is not None
    return key, elimination


FIXTURES = [
    pytest.param(a1_source, A1, A2, id="A1-recovered"),
    pytest.param(a2_source, A2, A1, id="A2-normal-coverage"),
]


@pytest.mark.parametrize(("make", "rule", "other"), FIXTURES)
def test_the_fixture_resolves_through_the_target_rule(make: Any, rule: Any, other: Any) -> None:
    diagnosis = _full(make())
    assert diagnosis.resolution is Resolution.AMBIGUOUS
    assert diagnosis.resolution_trace is not None
    assert diagnosis.resolution_trace.diagnosis_status == "SUPPORTED_CAUSE"
    assert diagnosis.root_cause is not None
    assert diagnosis.root_cause.canonical == "shop/Deployment/payment"
    _, elimination = _target(diagnosis, rule)
    assert elimination.decisive_evidence_ids


@pytest.mark.parametrize(("make", "rule", "other"), FIXTURES)
def test_t5_evidence_ablation_passes_when_d_is_necessary(make: Any, rule: Any, other: Any) -> None:
    key, elimination = _target(_full(make()), rule)
    result = evidence_ablation(
        make(), elimination.decisive_evidence_ids, hypothesis_key=key, rule=rule
    )
    assert result.verdict == PASS, result.reason
    assert result.diagnosis is not None and result.diagnosis.resolution is not Resolution.RESOLVED


@pytest.mark.parametrize(("make", "rule", "other"), FIXTURES)
def test_t5_fails_when_the_hidden_evidence_is_not_what_decided(
    make: Any, rule: Any, other: Any
) -> None:
    key, _ = _target(_full(make()), rule)
    unrelated = ("journal:shop/Service/checkout@0m",)
    result = evidence_ablation(make(), unrelated, hypothesis_key=key, rule=rule)
    assert result.verdict == FAIL
    assert evidence_ablation(make(), (), hypothesis_key=key, rule=rule).verdict == FAIL


def test_t5_mixed_provenance_is_ablation_ambiguous() -> None:
    key, elimination = _target(_full(a1_source()), A1)
    # The payment SPEC_CHANGE Finding cites @0m and @7m: hiding only @7m cannot be done safely.
    mixed = (*elimination.decisive_evidence_ids, "journal:shop/Deployment/payment@7m")
    result = evidence_ablation(a1_source(), mixed, hypothesis_key=key, rule=A1)
    assert result.verdict == ABLATION_AMBIGUOUS
    assert "SPEC_CHANGE:shop/Deployment/payment" in result.reason


@pytest.mark.parametrize(("make", "rule", "other"), FIXTURES)
def test_t6_rule_ablation_passes_and_disabling_another_rule_does_not(
    make: Any, rule: Any, other: Any
) -> None:
    result = rule_ablation(make(), rule)
    assert result.verdict == PASS, result.reason
    assert result.diagnosis is not None and result.diagnosis.resolution is not Resolution.RESOLVED
    assert rule_ablation(make(), other).verdict == FAIL  # the target rule is what resolved it


def test_the_ablation_config_digest_is_eval_only_and_deterministic() -> None:
    config = EngineConfig()
    product = rca_config_digest(config, None)
    a1 = eval_config_digest(config, EvalRuleOverrides(disabled_rules=(A1,)))
    assert a1 == eval_config_digest(EngineConfig(), EvalRuleOverrides(disabled_rules=(A1,)))
    assert a1 != eval_config_digest(config, EvalRuleOverrides(disabled_rules=(A2,)))
    assert a1 != product
    assert rca_config_digest(config, None) == product  # the product digest is untouched
    assert rule_ablation(a1_source(), A1).eval_config_digest == a1
    with pytest.raises(ValueError, match="no eval-only ablation"):
        EvalRuleOverrides(disabled_rules=(("m16.temporal-contradiction", "v1"),))


def test_production_has_no_rule_disable_surface() -> None:
    assert not any("disable" in name for name in EngineConfig.__dataclass_fields__)
    for directory in ("apps", "packages/rca", "packages/storage"):
        for path in (ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert "ablation" not in node.module, path


def test_t5_fails_when_the_ablated_diagnosis_still_resolves() -> None:
    key, _ = _target(_full(a1_source()), A1)
    # Hiding H_B's own manifestation removes the competitor: still RESOLVED, so T5 fails.
    result = evidence_ablation(a1_source(), ("event:b-unhealthy",), hypothesis_key=key, rule=A1)
    assert result.verdict == FAIL and "RESOLVED" in result.reason


def test_t5_fails_when_h_x_is_still_eliminated_by_the_target_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.evals.product import ablation  # noqa: PLC0415

    full = _full(a1_source())
    key, elimination = _target(full, A1)
    assert full.resolution_trace is not None
    still = full.model_copy(
        update={
            "resolution": Resolution.AMBIGUOUS,
            "resolution_trace": full.resolution_trace.model_copy(
                update={"state": Resolution.AMBIGUOUS}
            ),
        }
    )
    monkeypatch.setattr(ablation, "diagnose_case", lambda case, config=None: still)
    result = evidence_ablation(
        a1_source(), elimination.decisive_evidence_ids, hypothesis_key=key, rule=A1
    )
    assert result.verdict == FAIL and "still eliminated" in result.reason
