"""Offline tests for evaluator-only A1 compatibility target mapping."""

import pytest

from packages.evals.a1_targets import (
    A1_COMPATIBILITY_TARGETS,
    A1_TARGET_BY_SCENARIO,
    a1_compatibility_target_hash,
)
from packages.evals.dataset import FROZEN_DATASET
from packages.evals.live_fixtures import FIXTURE_BY_NAME
from packages.investigation import DependencyResourceId, WorkloadComponentId


def test_targets_are_one_to_one_with_frozen_dataset() -> None:
    assert [item.scenario_id for item in A1_COMPATIBILITY_TARGETS] == [
        item.scenario_id for item in FROZEN_DATASET
    ]
    assert len(A1_TARGET_BY_SCENARIO) == 10
    assert len({item.scenario_id for item in A1_COMPATIBILITY_TARGETS}) == 10
    assert len(a1_compatibility_target_hash()) == 64


def test_targets_validate_fixture_scope_and_non_obvious_resource_semantics() -> None:
    for target in A1_COMPATIBILITY_TARGETS:
        definition = FIXTURE_BY_NAME[target.fixture]
        assert target.alert_scope_component.value == definition.service
    assert A1_TARGET_BY_SCENARIO["V020-003"].symptom_component is WorkloadComponentId.ORDER_SERVICE
    assert A1_TARGET_BY_SCENARIO["V020-003"].causal_component is WorkloadComponentId.PAYMENT_SERVICE
    assert A1_TARGET_BY_SCENARIO["V020-005"].causal_resource is DependencyResourceId.POSTGRESQL
    assert A1_TARGET_BY_SCENARIO["V020-006"].causal_resource is DependencyResourceId.POSTGRESQL
    assert A1_TARGET_BY_SCENARIO["V020-007"].causal_resource is DependencyResourceId.KAFKA
    assert A1_TARGET_BY_SCENARIO["V020-008"].causal_resource is None


def test_target_builder_rejects_missing_target_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.evals import a1_targets

    monkeypatch.setitem(a1_targets._COMPATIBILITY_SPECS, "V020-001", {})
    with pytest.raises((KeyError, ValueError)):
        a1_targets._validate_compatibility_targets()


def test_target_builder_rejects_unknown_fixture_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.evals import a1_targets

    monkeypatch.delitem(FIXTURE_BY_NAME, "payment_error_spike")
    with pytest.raises(ValueError, match="fixture is not registered"):
        a1_targets._validate_compatibility_targets()
