"""M20.5 P2a: benchmark manifests name the snapshot normalization they were built from.

Comparisons can then tell "same RCA engine?" apart from "same source normalization?".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from itbench_builders import snapshot_scenario
from test_benchmark import _dataset
from test_investigation_benchmark import _FixtureDataset

from packages.evals.itbench.benchmark import predict
from packages.evals.itbench.dataset import ITBenchLiteDataset
from packages.evals.itbench.investigation_benchmark import predict_investigations
from packages.evals.itbench.source import SOURCE_NORMALIZATION


def test_the_normalization_tag_names_the_exact_instance_snapshot_source() -> None:
    assert SOURCE_NORMALIZATION == "itbench-snapshot-source.v3"


def test_the_prediction_manifest_records_the_source_normalization(tmp_path: Path) -> None:
    manifest: dict[str, Any] = predict(
        cast(ITBenchLiteDataset, _dataset(tmp_path)), ["Scenario-1"], tmp_path / "run", split="dev"
    )
    assert manifest["source_normalization"] == SOURCE_NORMALIZATION


def test_the_investigation_manifest_records_the_source_normalization(tmp_path: Path) -> None:
    scenario = snapshot_scenario(tmp_path)
    manifest = predict_investigations(
        _FixtureDataset(scenario), [scenario.scenario_id], tmp_path / "run", split="dev"
    )
    assert manifest["source_normalization"] == SOURCE_NORMALIZATION
