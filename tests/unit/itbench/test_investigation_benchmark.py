from __future__ import annotations

from pathlib import Path

import pytest
from itbench_builders import snapshot_scenario

from packages.evals.itbench.benchmark import BenchmarkError
from packages.evals.itbench.contracts import (
    ITBenchGroundTruth,
    ITBenchGroundTruthGroup,
    ITBenchScenario,
)
from packages.evals.itbench.investigation_benchmark import (
    _verify_investigation_seal,
    grade_investigations,
    predict_investigations,
)


class _FixtureDataset:
    def __init__(self, scenario: ITBenchScenario, out_dir: Path | None = None) -> None:
        self._scenario = scenario
        self._out_dir = out_dir
        self.ground_truth_reads = 0

    def scenario(self, scenario_id: str) -> ITBenchScenario:
        assert scenario_id == self._scenario.scenario_id
        return self._scenario

    def load_ground_truth(self, scenario_id: str) -> ITBenchGroundTruth:
        assert self._out_dir is not None
        _verify_investigation_seal(self._out_dir)
        self.ground_truth_reads += 1
        return ITBenchGroundTruth(
            scenario_id=scenario_id,
            root_cause_groups=(
                ITBenchGroundTruthGroup(
                    group_id="expected",
                    namespace="shop",
                    kind="Deployment",
                    name="checkout",
                    root_cause=True,
                ),
            ),
        )


def test_prediction_seals_runtime_metrics_without_opening_labels(tmp_path: Path) -> None:
    scenario = snapshot_scenario(tmp_path)
    out_dir = tmp_path / "run"
    dataset = _FixtureDataset(scenario)

    manifest = predict_investigations(dataset, [scenario.scenario_id], out_dir, split="dev")

    assert dataset.ground_truth_reads == 0
    assert manifest["ground_truth_read_during_prediction"] is False
    assert manifest["api_usage_class"] == "CLASS 0"
    assert manifest["real_provider_calls"] == 0
    assert manifest["metric_version"] == "m14.v1"
    verified = _verify_investigation_seal(out_dir)
    assert verified["scenario_ids"] == [scenario.scenario_id]
    prediction = out_dir / "predictions" / f"{scenario.scenario_id}.json"
    assert prediction.is_file()


def test_grader_checks_prediction_seal_before_reading_labels(tmp_path: Path) -> None:
    scenario = snapshot_scenario(tmp_path)
    out_dir = tmp_path / "run"
    dataset = _FixtureDataset(scenario, out_dir)
    predict_investigations(dataset, [scenario.scenario_id], out_dir, split="dev")

    result = grade_investigations(dataset, out_dir)

    assert dataset.ground_truth_reads == 1
    assert result["metric_version"] == "m14.v1"
    assert result["scenario_count"] == 1
    assert result["real_provider_calls"] == 0
    assert sum(result["outcomes"].values()) == 1
    assert (out_dir / "evaluation-seal.json").is_file()


def test_modified_prediction_fails_before_grader_reads_labels(tmp_path: Path) -> None:
    scenario = snapshot_scenario(tmp_path)
    out_dir = tmp_path / "run"
    dataset = _FixtureDataset(scenario, out_dir)
    predict_investigations(dataset, [scenario.scenario_id], out_dir, split="dev")
    prediction = out_dir / "predictions" / f"{scenario.scenario_id}.json"
    prediction.write_text("{}", encoding="utf-8")

    with pytest.raises(BenchmarkError, match="changed after sealing"):
        grade_investigations(dataset, out_dir)
    assert dataset.ground_truth_reads == 0
