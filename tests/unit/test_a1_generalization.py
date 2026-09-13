"""Offline tests for the A1 holdout definition and fixture separation."""

from packages.evals import (
    A1_GENERALIZATION_SCENARIOS,
    GENERALIZATION_FIXTURE_BY_NAME,
    generalization_dataset_hash,
    generalization_target_hash,
)
from packages.investigation import DependencyResourceId, WorkloadComponentId


def test_generalization_set_has_five_balanced_unseen_cases() -> None:
    assert [item.scenario_id for item in A1_GENERALIZATION_SCENARIOS] == [
        f"A1G-{index:03d}" for index in range(1, 5)
    ]
    assert sum(item.target.cross_component_opportunity for item in A1_GENERALIZATION_SCENARIOS) == 2
    assert (
        sum(not item.target.cross_component_opportunity for item in A1_GENERALIZATION_SCENARIOS)
        == 2
    )
    assert sum(item.target.change_evidence_opportunity for item in A1_GENERALIZATION_SCENARIOS) == 1
    assert sum(item.target.causal_resource is not None for item in A1_GENERALIZATION_SCENARIOS) == 2
    assert A1_GENERALIZATION_SCENARIOS[2].category == "same_component_negative_control"


def test_generalization_targets_use_registered_fixtures_and_canonical_identities() -> None:
    for scenario in A1_GENERALIZATION_SCENARIOS:
        definition = GENERALIZATION_FIXTURE_BY_NAME[scenario.fixture]
        assert definition.alert_name == scenario.alert_name
        assert scenario.target.alert_scope_component.value == definition.service
        assert scenario.target.symptom_component is scenario.alert_scope_component

    assert A1_GENERALIZATION_SCENARIOS[0].target.causal_component is (
        WorkloadComponentId.PAYMENT_SERVICE
    )
    assert A1_GENERALIZATION_SCENARIOS[1].target.causal_resource is (
        DependencyResourceId.POSTGRESQL
    )


def test_generalization_hashes_are_stable_and_distinct() -> None:
    dataset_hash = generalization_dataset_hash()
    target_hash = generalization_target_hash()

    assert len(dataset_hash) == 64
    assert len(target_hash) == 64
    assert dataset_hash != target_hash
