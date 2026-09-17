"""Predict, seal, and grade with a one-scenario dataset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from itbench_builders import snapshot_scenario

from packages.evals.itbench.benchmark import BenchmarkError, grade, load_split, predict
from packages.evals.itbench.contracts import (
    ITBenchGroundTruth,
    ITBenchGroundTruthGroup,
    ITBenchScenario,
)
from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.rca.agent import LLMInvestigator
from packages.rca.llm import ScriptedLLM


class FakeDataset:
    def __init__(self, scenario: ITBenchScenario, truth_name: str) -> None:
        self._scenario = scenario
        self.truth_name = truth_name
        self.truth_reads = 0

    def scenario(self, scenario_id: str) -> ITBenchScenario:
        assert scenario_id == self._scenario.scenario_id
        return self._scenario

    def load_ground_truth(self, scenario_id: str) -> ITBenchGroundTruth:
        self.truth_reads += 1
        return ITBenchGroundTruth(
            scenario_id=scenario_id,
            root_cause_groups=(
                ITBenchGroundTruthGroup(
                    group_id="root",
                    kind="ConfigMap",
                    namespace="shop",
                    name=self.truth_name,
                    root_cause=True,
                ),
            ),
        )


def _dataset(tmp_path: Path, truth_name: str = "flags") -> Any:
    return FakeDataset(snapshot_scenario(tmp_path / "data"), truth_name)


def test_prediction_never_reads_truth_and_grading_reports_the_funnel(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    out = tmp_path / "run"
    manifest = predict(cast(ITBenchLiteDataset, dataset), ["Scenario-1"], out, split="dev")
    assert dataset.truth_reads == 0
    assert manifest["ground_truth_read_during_prediction"] is False
    assert manifest["model_calls_total"] == 0
    assert manifest["mode"] == "deterministic" and manifest["model"] is None
    report = grade(cast(ITBenchLiteDataset, dataset), out)
    assert report["macro_f1"] == 1.0
    assert report["coverage_weighted_verified_f1"] == 1.0
    assert report["verified_count"] == 1
    assert report["verified_correct"] == 1
    assert report["verified_coverage"] == 1.0
    assert report["verified_accuracy"] == 1.0
    assert report["by_confidence"]["VERIFIED"] == {
        "count": 1,
        "correct": 1,
        "precision": 1.0,
        "share_of_predictions": 1.0,
    }
    assert report["funnel"] == {
        "root_cause_observable": 1.0,
        "in_top_5": 1.0,
        "in_top_3": 1.0,
        "top_1": 1.0,
    }
    assert report["rows"][0]["prediction"] == "shop/ConfigMap/flags"
    assert "| Scenario-1 | `shop/ConfigMap/flags` | VERIFIED | yes | 1 | yes |" in (
        out / "report.md"
    ).read_text(encoding="utf-8")


def test_wrong_and_unobservable_truth_is_reported(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, truth_name="missing")
    out = tmp_path / "run"
    predict(cast(ITBenchLiteDataset, dataset), ["Scenario-1"], out, split="dev")
    report = grade(cast(ITBenchLiteDataset, dataset), out)
    assert report["macro_f1"] == 0.0
    assert report["rows"][0]["observable"] is False
    assert report["rows"][0]["position"] is None


def test_tampered_or_repeated_runs_are_refused(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    out = tmp_path / "run"
    predict(cast(ITBenchLiteDataset, dataset), ["Scenario-1"], out, split="dev")
    with pytest.raises(BenchmarkError, match="already holds a sealed run"):
        predict(cast(ITBenchLiteDataset, dataset), ["Scenario-1"], out, split="dev")
    path = out / "predictions" / "Scenario-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["agent_output"]["reasoning"] = "edited"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="changed after sealing"):
        grade(cast(ITBenchLiteDataset, dataset), out)
    with pytest.raises(BenchmarkError, match="not sealed"):
        grade(cast(ITBenchLiteDataset, dataset), tmp_path / "empty")


def test_frozen_test_prediction_requires_a_clean_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import packages.evals.itbench.benchmark as benchmark

    monkeypatch.setattr(benchmark, "_git_dirty", lambda: True)
    with pytest.raises(BenchmarkError, match="clean repository"):
        predict(
            cast(ITBenchLiteDataset, _dataset(tmp_path)),
            ["Scenario-1"],
            tmp_path / "test-run",
            split="test",
        )


def test_split_file_is_disjoint_and_complete() -> None:
    dev, test = load_split("dev"), load_split("test")
    assert len(dev) == 10 and len(test) == 25
    assert not set(dev) & set(test)
    assert len(load_split("all")) == 35
    with pytest.raises(BenchmarkError):
        load_split("train")


def test_cli_refuses_the_test_split_without_confirmation(tmp_path: Path) -> None:
    from apps.cli.main import main

    out = tmp_path / "x"
    code = main(["eval", "--split", "test", "--out", str(out), "--dataset", str(tmp_path)])
    assert code == 2
    assert not out.exists()


def test_investigator_failures_are_counted_and_flagged(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    out = tmp_path / "run"
    investigator = LLMInvestigator(ScriptedLLM(replies=[]))
    manifest = predict(
        cast(ITBenchLiteDataset, dataset),
        ["Scenario-1"],
        out,
        split="dev",
        investigator=investigator,
    )
    assert manifest["investigator_errors"] == 1
    assert manifest["records"][0]["investigator_error"] == "scripted replies exhausted"
    grade(cast(ITBenchLiteDataset, dataset), out)
    assert "not a valid investigator run" in (out / "report.md").read_text(encoding="utf-8")
