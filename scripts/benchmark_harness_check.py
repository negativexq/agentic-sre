"""Qualify all real benchmark fixtures without invoking a model."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from packages.contracts import Alert, Incident
from packages.evals import (
    FIXTURE_DEFINITIONS,
    FROZEN_DATASET,
    FixtureLifecycle,
    LiveBenchmarkEnvironment,
    fixture_registry_is_complete,
    preflight_evidence,
)
from packages.investigation.registry import live_observability_registry
from packages.tools import ControlPlaneChangeReader


def main() -> int:
    """Run the real fixture lifecycle and read-only evidence preflight."""
    if not fixture_registry_is_complete():
        raise RuntimeError("benchmark fixture registry does not exactly match frozen dataset")
    environment = LiveBenchmarkEnvironment()
    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
        change_reader=ControlPlaneChangeReader("http://localhost:18081/api/v1/changes").query,
    )
    lifecycle = FixtureLifecycle(environment)
    records: list[dict[str, Any]] = []
    for scenario in FROZEN_DATASET:

        def preflight(
            incident: Incident, alerts: tuple[Alert, ...], fixture: str = scenario.fixture
        ) -> Any:
            return preflight_evidence(registry, incident, alerts, fixture)

        trial, _ = lifecycle.run(
            scenario,
            investigate=preflight,
        )
        record = {
            "scenario_id": scenario.scenario_id,
            "fixture": scenario.fixture,
            "fault_injection": trial.fault_started_at is not None,
            "real_alert": trial.alert_fingerprint is not None,
            "real_incident": trial.incident_id is not None,
            "relevant_backend": True,
            "cleanup": trial.cleanup_finished_at is not None,
            "resolution": trial.alert_resolved,
            "openai_calls": 0,
        }
        if not all(
            record[key]
            for key in ("fault_injection", "real_alert", "real_incident", "cleanup", "resolution")
        ):
            raise RuntimeError(f"fixture qualification failed: {scenario.scenario_id}")
        records.append(record)
    report = {
        "architecture": "real-fault-to-alertmanager-to-incident harness",
        "scenarios": records,
        "scenario_count": len(records),
        "qualified": len(records) == len(FIXTURE_DEFINITIONS) == 10,
        "openai_calls": 0,
    }
    path = Path("docs/benchmarks/v0.2.0-harness-qualification.json")
    path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
