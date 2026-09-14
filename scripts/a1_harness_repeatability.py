"""Repeat the complete frozen fault-to-incident suite without a model."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.evals import (
    A1_GENERALIZATION_SCENARIOS,
    FIXTURE_BY_NAME,
    FROZEN_DATASET,
    GENERALIZATION_FIXTURE_BY_NAME,
    FixtureLifecycle,
    LiveBenchmarkEnvironment,
    PhaseLedger,
    preflight_evidence,
)
from packages.investigation.registry import live_observability_registry
from packages.tools import ControlPlaneChangeReader

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/benchmarks/a1-r4-harness-reliability-v2.json"
SCENARIOS: tuple[Any, ...] = (*FROZEN_DATASET, *A1_GENERALIZATION_SCENARIOS)
DEFINITIONS: dict[str, Any] = {**FIXTURE_BY_NAME, **GENERALIZATION_FIXTURE_BY_NAME}


def _record(scenario: Any, trial: Any, baseline: Any, started: float) -> dict[str, Any]:
    fault = trial.fault_oracle
    trigger = trial.trigger_oracle
    workload_pass = (
        trial.workload is not None
        and trial.workload.successful == trial.workload.attempted
        and trial.workload.failed == 0
    ) or (trial.workload is None and scenario.fixture == "payment_pod_crash")
    return {
        "scenario_id": scenario.scenario_id,
        "fixture": scenario.fixture,
        "baseline_pass": bool(baseline and baseline.passed),
        "fault_oracle_pass": bool(fault and fault.passed),
        "workload_oracle_pass": workload_pass,
        "trigger_oracle_pass": bool(trigger and trigger.passed),
        "alert_pass": trial.alert_fingerprint is not None,
        "incident_pass": trial.incident_id is not None,
        "recovery_pass": trial.alert_resolved and trial.baseline_restored,
        "duration_seconds": max(time.monotonic() - started, 0.0),
        "trial": trial.model_dump(mode="json"),
        "status": "PASS"
        if trial.incident_id is not None
        and trial.cleanup_finished_at is not None
        and trial.alert_resolved
        and trial.baseline_restored
        else "FAIL",
    }


def main() -> int:
    repeats = int(os.getenv("A1_HARNESS_SUITE_REPEATS", "3"))
    if repeats < 1:
        raise ValueError("A1_HARNESS_SUITE_REPEATS must be positive")
    output = Path(os.getenv("A1_HARNESS_REPEATABILITY_OUTPUT", str(OUTPUT)))
    environment = LiveBenchmarkEnvironment()
    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
        change_reader=ControlPlaneChangeReader("http://localhost:18081/api/v1/changes").query,
    )
    records: list[dict[str, Any]] = []
    status = "PASS"
    failure: dict[str, str] | None = None
    run_id = f"a1-r4-reliability-{time.time_ns()}"
    phase_dir = ROOT / ".local" / "a1-r4-harness-reliability" / run_id
    phase_path = phase_dir / "phases.jsonl"
    try:
        ledger = PhaseLedger(phase_path, execution_id=run_id)
        for suite in range(1, repeats + 1):
            environment.prepare_benchmark_state(f"{run_id}-suite-{suite}")
            lifecycle = FixtureLifecycle(
                environment,
                definitions=DEFINITIONS,
                phase_ledger=ledger,
            )
            for scenario in SCENARIOS:
                environment.prepare_benchmark_state(
                    f"{run_id}-suite-{suite}-{scenario.scenario_id}"
                )
                prepare_duration = environment.last_prepare_duration_seconds
                started = time.monotonic()
                trial = None
                try:

                    def investigate(
                        incident: Any,
                        alerts: tuple[Any, ...],
                        fixture: str = scenario.fixture,
                    ) -> Any:
                        return preflight_evidence(
                            registry,
                            incident,
                            alerts,
                            fixture,
                            definitions=DEFINITIONS,
                        )

                    trial, evidence = lifecycle.run(scenario, investigate=investigate)
                    record = _record(
                        scenario,
                        trial,
                        environment.last_baseline_oracle,
                        started,
                    )
                    record["evidence_oracle_pass"] = evidence is not None and len(evidence) > 0
                except Exception as error:
                    record = {
                        "scenario_id": scenario.scenario_id,
                        "fixture": scenario.fixture,
                        "suite": suite,
                        "status": "FAIL",
                        "error_code": str(getattr(error, "code", type(error).__name__)),
                        "error": str(error)[:500],
                        "trial": trial.model_dump(mode="json") if trial is not None else None,
                    }
                record["suite"] = suite
                record["prepare_duration_seconds"] = prepare_duration
                records.append(record)
                print(json.dumps(record, sort_keys=True), flush=True)
                if record["status"] != "PASS":
                    raise RuntimeError(
                        f"A1 harness reliability failed: suite {suite}/{scenario.scenario_id}"
                    )
    except Exception as error:
        status = "FAIL"
        failure = {"error_code": type(error).__name__, "error": str(error)[:500]}
    report = {
        "artifact_type": "A1_R4_HARNESS_RELIABILITY",
        "status": status,
        "model_calls": 0,
        "scenario_order": [item.scenario_id for item in SCENARIOS],
        "suite_repeat_count": repeats,
        "scenarios_per_suite": len(SCENARIOS),
        "total_scenario_executions": len(records),
        "infrastructure_failure_count": sum(item["status"] != "PASS" for item in records),
        "reset_strategy": "control-plane reset plus baseline oracle and bounded telemetry windows",
        "failure": failure,
        "run_id": run_id,
        "phase_ledger_path": str(phase_path.relative_to(ROOT)),
        "reset_duration_seconds": {
            "mean": (
                sum(
                    float(item["prepare_duration_seconds"])
                    for item in records
                    if item.get("prepare_duration_seconds") is not None
                )
                / max(
                    sum(item.get("prepare_duration_seconds") is not None for item in records),
                    1,
                )
            ),
            "max": max(
                (
                    float(item["prepare_duration_seconds"])
                    for item in records
                    if item.get("prepare_duration_seconds") is not None
                ),
                default=0.0,
            ),
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if status == "PASS":
        print(f"A1 harness reliability: PASS ({len(records)} scenarios, 0 model calls)")
        return 0
    print(f"A1 harness reliability: FAIL ({len(records)} records, 0 model calls)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
