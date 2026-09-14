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
    supervisor = Path("scripts/live_forward_supervisor.py").read_text(encoding="utf-8")
    target = makefile.split("a1-live-benchmark:\n", 1)[1].split("\n\n", 1)[0]

    assert "live_forward_supervisor.py --profile a1" in target
    assert '"control-plane",\n        "sre-demo",\n        "control-plane"' in supervisor
    assert '"prometheus", "observability", "prometheus"' in supervisor


def test_a1_generalization_uses_reconnecting_observability_forwards() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    target = makefile.split("a1-generalization-check:\n", 1)[1].split("\n\n", 1)[0]
    supervisor = Path("scripts/live_forward_supervisor.py").read_text(encoding="utf-8")

    assert "live_forward_supervisor.py --profile a1" in target
    assert "def _restart_dead" in supervisor
    assert "wait_until_ready" in supervisor


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

    assert "scripts/live_forward_supervisor.py --profile a1" in target


def test_smoke_is_self_contained_and_excluded_from_frozen_targets() -> None:
    from packages.evals import A1_TARGET_BY_SCENARIO, FROZEN_DATASET
    from packages.evals.live_fixtures import FIXTURE_DEFINITIONS
    from packages.evals.smoke import SMOKE_ALERT_NAME, SMOKE_SCENARIO

    assert SMOKE_SCENARIO.scenario_id not in {item.scenario_id for item in FROZEN_DATASET}
    assert SMOKE_SCENARIO.scenario_id not in A1_TARGET_BY_SCENARIO
    assert SMOKE_ALERT_NAME not in {item.alert_name for item in FIXTURE_DEFINITIONS}


def test_zero_model_smoke_has_no_provider_construction_before_lifecycle() -> None:
    source = Path("scripts/a1_r4_smoke_qualification.py").read_text(encoding="utf-8")
    live_source = Path("scripts/a1_live_benchmark.py").read_text(encoding="utf-8")

    assert "OpenAIProvider" not in source
    assert "FixtureLifecycle" in source
    assert "SMOKE_SCENARIO" in live_source
    assert "existing incident" not in live_source


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
