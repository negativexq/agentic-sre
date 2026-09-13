"""Qualify all real benchmark fixtures without invoking a model."""

from __future__ import annotations

import json
import os
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
    select_harness_scenarios,
)
from packages.investigation.registry import live_observability_registry
from packages.tools import ControlPlaneChangeReader


def selected_scenarios() -> tuple[Any, ...]:
    """Return validated development selections without changing the frozen dataset."""
    return select_harness_scenarios(os.getenv("SRE_HARNESS_SCENARIOS"))


def main() -> int:
    """Run the real fixture lifecycle and read-only evidence preflight."""
    if not fixture_registry_is_complete():
        raise RuntimeError("benchmark fixture registry does not exactly match frozen dataset")
    environment = LiveBenchmarkEnvironment()
    environment.prepare_benchmark_state("v0.2-harness-qualification")
    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
        change_reader=ControlPlaneChangeReader("http://localhost:18081/api/v1/changes").query,
    )
    lifecycle = FixtureLifecycle(environment)
    records: list[dict[str, Any]] = []
    scenarios = selected_scenarios()
    filtered = len(scenarios) != len(FROZEN_DATASET)
    for scenario in scenarios:

        def preflight(
            incident: Incident, alerts: tuple[Alert, ...], fixture: str = scenario.fixture
        ) -> Any:
            return preflight_evidence(registry, incident, alerts, fixture)

        trial, evidence = lifecycle.run(
            scenario,
            investigate=preflight,
        )
        record = {
            "scenario_id": scenario.scenario_id,
            "fixture": scenario.fixture,
            "fault_injection": trial.fault_started_at is not None,
            "telemetry": evidence is not None and len(evidence) > 0,
            "real_alert": trial.alert_fingerprint is not None,
            "real_incident": trial.incident_id is not None,
            "evidence": evidence is not None and len(evidence) > 0,
            "cleanup": trial.cleanup_finished_at is not None,
            "resolution": trial.alert_resolved,
            "openai_calls": 0,
        }
        if not all(
            record[key]
            for key in (
                "fault_injection",
                "telemetry",
                "real_alert",
                "real_incident",
                "evidence",
                "cleanup",
                "resolution",
            )
        ):
            raise RuntimeError(f"fixture qualification failed: {scenario.scenario_id}")
        records.append(record)
        print(f"{scenario.scenario_id} PASS", flush=True)
    if filtered:
        print(json.dumps({"selected": [item.scenario_id for item in scenarios], "qualified": True}))
        return 0
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
