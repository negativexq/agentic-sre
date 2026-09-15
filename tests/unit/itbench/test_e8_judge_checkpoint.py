"""Offline tests for E8 per-scenario judge checkpoint semantics."""

from __future__ import annotations

import json
from pathlib import Path

from packages.evals.itbench.judge_checkpoint import checkpoint_is_durable, next_pending_scenario


def test_durable_successful_checkpoint_is_not_rerun(tmp_path: Path) -> None:
    checkpoint = tmp_path / "Scenario-1" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "scenario": "Scenario-1",
                "evaluation_success": True,
                "root_cause_entity_f1": 0.0,
            }
        ),
        encoding="utf-8",
    )
    assert checkpoint_is_durable(checkpoint)
    assert next_pending_scenario(tmp_path) == "Scenario-2"


def test_incomplete_checkpoint_remains_pending(tmp_path: Path) -> None:
    checkpoint = tmp_path / "Scenario-1" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps({"scenario": "Scenario-1", "evaluation_success": False}),
        encoding="utf-8",
    )
    assert not checkpoint_is_durable(checkpoint)
    assert next_pending_scenario(tmp_path) == "Scenario-1"
