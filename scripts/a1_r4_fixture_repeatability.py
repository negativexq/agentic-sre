"""Repeat timing-sensitive A1 fixtures through the real path without a model."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.evals import (
    FROZEN_DATASET,
    FixtureLifecycle,
    LiveBenchmarkEnvironment,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/benchmarks/a1-r4-fixture-repeatability.json"
V007_REPEATS = 10
TIMING_SENSITIVE_FIXTURES = ("payment_pod_crash", "payment_config_change")
TIMING_SENSITIVE_REPEATS = 5


def _scenario_for(fixture: str) -> Any:
    return next(item for item in FROZEN_DATASET if item.fixture == fixture)


def _record(
    scenario: Any,
    repeat: int,
    trial: Any | None,
    *,
    error: Exception | None = None,
) -> dict[str, Any]:
    stimulus = getattr(trial, "stimulus", None) if trial is not None else None
    return {
        "scenario_id": scenario.scenario_id,
        "fixture": scenario.fixture,
        "repeat": repeat,
        "stimulus": stimulus.model_dump(mode="json") if stimulus is not None else None,
        "prometheus_firing": (
            stimulus.prometheus_firing_observed if stimulus is not None else None
        ),
        "incident": trial.incident_id is not None if trial is not None else False,
        "cleanup": trial.cleanup_finished_at is not None if trial is not None else False,
        "alert_resolved": trial.alert_resolved if trial is not None else False,
        "baseline_restored": trial.baseline_restored if trial is not None else False,
        "status": "PASS"
        if error is None
        and trial is not None
        and trial.incident_id is not None
        and trial.cleanup_finished_at is not None
        and trial.alert_resolved
        and trial.baseline_restored
        else "FAIL",
        "error": str(error) if error is not None else None,
    }


def main() -> int:
    output = Path(os.getenv("A1_R4_REPEATABILITY_OUTPUT", str(OUTPUT)))
    environment = LiveBenchmarkEnvironment()
    environment.prepare_benchmark_state("a1-r4-fixture-repeatability")
    lifecycle = FixtureLifecycle(environment)
    mode = os.getenv("A1_R4_REPEATABILITY_MODE", "all")
    if mode not in {"all", "timing", "v007", "v009", "v010"}:
        raise ValueError("A1_R4_REPEATABILITY_MODE must be all, timing, v007, v009, or v010")
    plans: list[tuple[str, int]] = []
    if mode in {"all", "v007"}:
        plans.append(("order_worker_lag", V007_REPEATS))
    if mode in {"all", "timing", "v009"}:
        plans.append((TIMING_SENSITIVE_FIXTURES[0], TIMING_SENSITIVE_REPEATS))
    if mode in {"all", "timing", "v010"}:
        plans.append((TIMING_SENSITIVE_FIXTURES[1], TIMING_SENSITIVE_REPEATS))
    records: list[dict[str, Any]] = []
    try:
        for fixture, repeats in plans:
            scenario = _scenario_for(fixture)
            for repeat in range(1, repeats + 1):
                environment.prepare_benchmark_state(f"a1-r4-{scenario.scenario_id}-{repeat}")
                trial = None
                try:
                    trial, _ = lifecycle.run(scenario)
                    record = _record(scenario, repeat, trial)
                except Exception as error:
                    record = _record(scenario, repeat, trial, error=error)
                records.append(record)
                print(json.dumps(record, sort_keys=True), flush=True)
                if record["status"] != "PASS":
                    raise RuntimeError(f"repeatability failed: {scenario.scenario_id}/{repeat}")
    finally:
        report = {
            "artifact_type": "A1_R4_FIXTURE_REPEATABILITY",
            "model_calls": 0,
            "generated_at": datetime.now(UTC).isoformat(),
            "plans": [{"fixture": fixture, "repeat_count": repeats} for fixture, repeats in plans],
            "records": records,
            "pass_count": sum(item["status"] == "PASS" for item in records),
            "record_count": len(records),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
