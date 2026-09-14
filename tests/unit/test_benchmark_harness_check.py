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


def test_a1_generalization_uses_reconnecting_observability_forwards() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    target = makefile.split("a1-generalization-check:\n", 1)[1].split("\n\n", 1)[0]

    assert "while true; do kubectl port-forward -n observability svc/prometheus" in target
    assert "while true; do kubectl port-forward -n observability svc/alertmanager" in target


def test_a1_smoke_paths_are_configurable_for_new_execution_revisions() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    target = makefile.split("a1-live-smoke:\n", 1)[1].split("\n\n", 1)[0]

    assert "A1_LIVE_MANIFEST" in target
    assert "A1_LIVE_SMOKE" in target
    assert "A1_LIVE_SMOKE_PHASE_LEDGER" in target
    assert "A1_LIVE_BUDGET_FILE" in target
    assert "SRE_A1_LEDGER_PATH" in target


def test_a1_benchmark_paths_are_configurable_for_new_execution_revisions() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    target = makefile.split("a1-live-benchmark:\n", 1)[1].split("\n\n", 1)[0]

    assert "A1_LIVE_MANIFEST" in target
    assert "A1_LIVE_RESULT" in target
    assert "A1_LIVE_BUDGET_FILE" in target
    assert "SRE_A1_LEDGER_PATH" in target


@pytest.mark.parametrize("target_name", ["a1-live-smoke", "a1-live-benchmark"])
def test_a1_live_targets_reconnect_observability_forwards(target_name: str) -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    target = makefile.split(f"{target_name}:\n", 1)[1].split("\n\n", 1)[0]

    assert "while true; do kubectl port-forward -n observability svc/prometheus" in target


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


def test_zero_model_repeatability_script_has_no_openai_provider_path() -> None:
    source = Path("scripts/a1_harness_repeatability.py").read_text(encoding="utf-8")

    assert "OpenAIProvider" not in source
    assert "OPENAI_API_KEY" not in source
