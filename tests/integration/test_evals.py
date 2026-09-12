"""Frozen dataset and zero-credit grader tests."""

from packages.evals import (
    FROZEN_DATASET,
    capability_matrix,
    frozen_dataset_hash,
    run_offline_benchmark,
)
from packages.investigation.registry import live_observability_registry


def test_frozen_dataset_is_ten_cases_with_stable_hash() -> None:
    """The baseline dataset is versioned and reproducible."""
    assert len(FROZEN_DATASET) == 10
    assert frozen_dataset_hash() == frozen_dataset_hash()
    assert all("mechanism" not in item.public_context() for item in FROZEN_DATASET)


def test_offline_benchmark_uses_zero_live_api_calls() -> None:
    """The scripted benchmark exercises runtime and graders without OpenAI."""
    report = run_offline_benchmark()

    assert report.scenario_count == 10
    assert report.completion_rate == 1
    assert report.composite_rca == 1
    assert report.valid_evidence_reference_rate == 1
    assert report.total_live_api_calls == 0


def test_live_registry_covers_every_frozen_scenario_category() -> None:
    """The live registry must expose evidence paths before paid benchmarking."""
    registry = live_observability_registry("http://prometheus", "http://loki", "http://tempo")
    matrix = capability_matrix(registry=registry)
    assert len(matrix) == 10
    assert all(item.available for item in matrix)


def test_live_registry_is_bounded_and_write_free() -> None:
    """Every exposed capability is descriptive and read-only."""
    registry = live_observability_registry("http://prometheus", "http://loki", "http://tempo")
    forbidden = {"kubectl", "shell", "bash", "delete", "patch", "apply", "rollback", "scale"}
    for descriptor in registry.descriptors():
        assert descriptor["name"]
        assert descriptor["version"]
        assert descriptor["purpose"]
        assert descriptor["evidence_type"]
        assert descriptor["arguments"] is not None
        haystack = descriptor["name"].lower()
        assert haystack not in forbidden
