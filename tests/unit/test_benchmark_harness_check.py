"""Offline tests for focused zero-LLM harness qualification."""

import pytest

from packages.evals.live_fixtures import select_harness_scenarios


def test_harness_filter_selects_frozen_scenario() -> None:
    selected = select_harness_scenarios("V020-009,V020-010")

    assert [item.scenario_id for item in selected] == ["V020-009", "V020-010"]


def test_harness_filter_rejects_unknown_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="unknown harness scenarios"):
        select_harness_scenarios("V020-999")
