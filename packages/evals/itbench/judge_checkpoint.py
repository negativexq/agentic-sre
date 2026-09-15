"""Offline-safe checkpoint primitives for per-scenario ITBench judging."""

from __future__ import annotations

import json
from pathlib import Path

SCENARIOS = (
    "Scenario-1",
    "Scenario-2",
    "Scenario-4",
    "Scenario-5",
    "Scenario-6",
    "Scenario-7",
    "Scenario-8",
    "Scenario-9",
    "Scenario-11",
    "Scenario-12",
    "Scenario-13",
    "Scenario-14",
    "Scenario-15",
    "Scenario-16",
    "Scenario-17",
    "Scenario-18",
    "Scenario-19",
    "Scenario-20",
    "Scenario-21",
    "Scenario-22",
    "Scenario-23",
    "Scenario-24",
    "Scenario-25",
    "Scenario-29",
    "Scenario-31",
    "Scenario-33",
    "Scenario-34",
    "Scenario-35",
    "Scenario-38",
    "Scenario-80",
    "Scenario-81",
    "Scenario-83",
    "Scenario-91",
    "Scenario-102",
    "Scenario-105",
)


def checkpoint_path(root: Path, scenario: str) -> Path:
    return root / scenario / "checkpoint.json"


def checkpoint_is_durable(path: Path) -> bool:
    """A durable successful checkpoint is sufficient to skip paid work."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("evaluation_success") is True
        and payload.get("scenario") in SCENARIOS
        and isinstance(payload.get("root_cause_entity_f1"), (int, float))
    )


def next_pending_scenario(root: Path) -> str | None:
    return next(
        (
            scenario
            for scenario in SCENARIOS
            if not checkpoint_is_durable(checkpoint_path(root, scenario))
        ),
        None,
    )


__all__ = ["SCENARIOS", "checkpoint_is_durable", "checkpoint_path", "next_pending_scenario"]
