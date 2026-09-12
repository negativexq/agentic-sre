"""Frozen dataset and zero-credit grader tests."""

from packages.changes.service import ChangeService
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
    assert all(item.available for item in matrix if item.category != "deployment")
    assert not next(item for item in matrix if item.category == "deployment").available


def test_live_registry_marks_deployment_capability_only_with_historical_reader() -> None:
    """Deployment/configuration readiness requires a real change-journal source."""
    journal = ChangeService()
    registry = live_observability_registry(
        "http://prometheus",
        "http://loki",
        "http://tempo",
        change_reader=lambda _operation, _parameters: tuple(journal.recent()),
    )
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


def test_all_live_tool_descriptors_match_their_canonical_argument_models() -> None:
    """The 17-tool catalog is generated from the same validators used at runtime."""
    registry = live_observability_registry("http://prometheus", "http://loki", "http://tempo")
    descriptors = {item["name"]: item for item in registry.descriptors()}
    assert len(descriptors) == 17
    for name in registry.names():
        tool = registry.get(name)
        descriptor = descriptors[name]
        expected = tool.argument_model
        schema = expected.model_json_schema()
        assert set(descriptor["arguments"]) == set(schema.get("properties", {}))
        assert all(
            descriptor["arguments"][field]["required"] == (field in schema.get("required", []))
            for field in schema.get("properties", {})
        )
        assert tool.validate_arguments(
            {
                field: (
                    "order-worker" if field in {"service", "consumer", "deployment"} else "0" * 32
                )
                for field, definition in descriptor["arguments"].items()
                if definition["required"]
            }
        )


def test_cross_tool_fields_are_rejected_by_canonical_runtime_contracts() -> None:
    """Tool-specific arguments cannot leak across otherwise similar operations."""
    registry = live_observability_registry("http://prometheus", "http://loki", "http://tempo")
    invalid = (
        ("service_logs", {"service": "order-worker", "pattern": "ERROR"}),
        ("service_error_logs", {"service": "order-worker", "range_seconds": 300}),
        ("kafka_consumer_lag", {"consumer": "order-worker", "pattern": "ERROR"}),
        ("trace_detail", {"trace_id": "0" * 32, "service": "order-worker"}),
        ("kubernetes_deployment", {"deployment": "payment-service", "service": "payment-service"}),
    )
    for name, arguments in invalid:
        try:
            registry.validate(name, arguments)
        except ValueError:
            continue
        raise AssertionError(f"invalid cross-tool fields accepted by {name}")
