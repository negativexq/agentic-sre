"""Offline tests for focused zero-LLM harness qualification."""

import importlib
import json
from pathlib import Path

import pytest

from packages.evals.live_fixtures import BenchmarkStateContaminatedError, select_harness_scenarios


def test_harness_filter_selects_frozen_scenario() -> None:
    selected = select_harness_scenarios("V020-009,V020-010")

    assert [item.scenario_id for item in selected] == ["V020-009", "V020-010"]


def test_harness_filter_rejects_unknown_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="unknown harness scenarios"):
        select_harness_scenarios("V020-999")


def test_a1_benchmark_forwards_control_plane_in_sre_demo_namespace() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    target = makefile.split("a1-live-benchmark:\n", 1)[1].split("\n\n", 1)[0]

    assert "kubectl port-forward -n sre-demo svc/control-plane" in target
    assert "kubectl port-forward -n observability svc/control-plane" not in target


def test_a1_benchmark_prepares_state_before_budget_or_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Contamination aborts before the benchmark can initialize its provider/budget."""
    benchmark = importlib.import_module("scripts.a1_live_benchmark")

    smoke_path = tmp_path / "smoke.json"
    smoke_path.write_text(
        json.dumps({"transport_pass": True, "artifact_pass": True}), encoding="utf-8"
    )
    monkeypatch.setattr(benchmark, "SMOKE_PATH", smoke_path)

    class FakeEnvironment:
        def prepare_benchmark_state(self, _execution_id: str) -> None:
            raise BenchmarkStateContaminatedError("contaminated")

    class UnexpectedBudget:
        @classmethod
        def from_environment(cls, **_: object) -> object:
            raise AssertionError("budget/provider path must not start")

    monkeypatch.setattr(benchmark, "LiveBenchmarkEnvironment", FakeEnvironment)
    monkeypatch.setattr(benchmark, "LiveModelBudget", UnexpectedBudget)

    with pytest.raises(BenchmarkStateContaminatedError, match="contaminated"):
        benchmark._benchmark({"experiment_id": "test-execution"})
