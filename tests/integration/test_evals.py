"""Frozen dataset and zero-credit grader tests."""

from packages.evals import FROZEN_DATASET, frozen_dataset_hash, run_offline_benchmark


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
