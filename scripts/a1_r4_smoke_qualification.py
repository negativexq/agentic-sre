"""Qualify the integration-only A1 smoke fixture without a model."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.contracts import Alert, Incident
from packages.evals import FixtureLifecycle, LiveBenchmarkEnvironment, preflight_evidence
from packages.evals.smoke import SMOKE_FIXTURE_DEFINITION, SMOKE_SCENARIO
from packages.investigation.registry import live_observability_registry
from packages.tools import ControlPlaneChangeReader

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/benchmarks/a1-r4-smoke-fixture-qualification.json"
REPEATS = 5


def main() -> int:
    output = Path(os.getenv("A1_R4_SMOKE_QUALIFICATION_OUTPUT", str(OUTPUT)))
    environment = LiveBenchmarkEnvironment()
    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
        change_reader=ControlPlaneChangeReader("http://localhost:18081/api/v1/changes").query,
    )
    definitions = {SMOKE_FIXTURE_DEFINITION.fixture: SMOKE_FIXTURE_DEFINITION}
    records: list[dict[str, Any]] = []
    for repeat in range(1, REPEATS + 1):
        environment.prepare_benchmark_state(f"a1-r4-smoke-qualification-{repeat}")
        lifecycle = FixtureLifecycle(environment, definitions=definitions)

        def investigate(incident: Incident, alerts: tuple[Alert, ...]) -> Any:
            return preflight_evidence(
                registry,
                incident,
                alerts,
                SMOKE_FIXTURE_DEFINITION.fixture,
                definitions=definitions,
            )

        trial = None
        error: Exception | None = None
        try:
            trial, evidence = lifecycle.run(SMOKE_SCENARIO, investigate=investigate)
            if not evidence:
                raise RuntimeError("smoke evidence preflight returned no evidence")
            if trial.incident_id is None or not trial.alert_resolved or not trial.baseline_restored:
                raise RuntimeError("smoke lifecycle did not recover a complete incident")
        except Exception as exc:  # pragma: no cover - live qualification path
            error = exc
        record = {
            "repeat": repeat,
            "scenario_id": SMOKE_SCENARIO.scenario_id,
            "fixture": SMOKE_FIXTURE_DEFINITION.fixture,
            "alert_name": SMOKE_FIXTURE_DEFINITION.alert_name,
            "baseline": bool(
                environment.last_baseline_oracle and environment.last_baseline_oracle.passed
            ),
            "fault": trial is not None and trial.fault_started_at is not None,
            "workload": trial is not None
            and trial.workload is not None
            and trial.workload.failed == 0,
            "trigger": trial is not None and trial.trigger_oracle is not None,
            "prometheus": trial is not None and trial.alert_fingerprint is not None,
            "alertmanager": trial is not None and trial.alert_fingerprint is not None,
            "incident": trial is not None and trial.incident_id is not None,
            "evidence_preflight": error is None,
            "recovery": trial is not None and trial.alert_resolved and trial.baseline_restored,
            "status": "PASS" if error is None else "FAIL",
            "error": str(error) if error else None,
        }
        records.append(record)
        print(json.dumps(record, sort_keys=True), flush=True)
        if error is not None:
            raise error
    report = {
        "artifact_type": "A1_R4_SMOKE_FIXTURE_QUALIFICATION",
        "purpose": "integration-only real fault qualification; no model quality evaluation",
        "smoke_fixture": SMOKE_SCENARIO.scenario_id,
        "alert_name": SMOKE_FIXTURE_DEFINITION.alert_name,
        "repeat_count": REPEATS,
        "pass_count": sum(item["status"] == "PASS" for item in records),
        "record_count": len(records),
        "openai_calls": 0,
        "generated_at": datetime.now(UTC).isoformat(),
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
