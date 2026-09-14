"""Check selected fixture orderings through the real path without a model."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.evals import (
    FIXTURE_BY_NAME,
    FROZEN_DATASET,
    FixtureLifecycle,
    LiveBenchmarkEnvironment,
    preflight_evidence,
)
from packages.investigation.registry import live_observability_registry
from packages.tools import ControlPlaneChangeReader

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/benchmarks/a1-r4-order-independence.json"
DEFAULT_SEQUENCES = (("V020-006", "V020-007"), ("V020-007", "V020-008"))


def _sequences() -> tuple[tuple[str, ...], ...]:
    raw = os.getenv("A1_ORDER_INDEPENDENCE_SEQUENCES")
    if not raw:
        return DEFAULT_SEQUENCES + tuple(tuple(reversed(item)) for item in DEFAULT_SEQUENCES)
    sequences: list[tuple[str, ...]] = []
    by_id = {item.scenario_id: item for item in FROZEN_DATASET}
    for value in raw.split(";"):
        ids = tuple(item.strip() for item in value.split(",") if item.strip())
        if len(ids) < 2 or any(item not in by_id for item in ids):
            raise ValueError(f"invalid A1_ORDER_INDEPENDENCE_SEQUENCES value: {value}")
        sequences.append(ids)
    return tuple(sequences)


def _record(scenario: Any, trial: Any, elapsed: float) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "fixture": scenario.fixture,
        "incident": trial.incident_id is not None,
        "alert": trial.alert_fingerprint is not None,
        "recovery": trial.alert_resolved,
        "baseline_restored": trial.baseline_restored,
        "duration_seconds": elapsed,
        "status": "PASS"
        if trial.incident_id is not None
        and trial.alert_fingerprint is not None
        and trial.alert_resolved
        and trial.baseline_restored
        else "FAIL",
    }


def main() -> int:
    sequences = _sequences()
    by_id = {item.scenario_id: item for item in FROZEN_DATASET}
    environment = LiveBenchmarkEnvironment()
    registry = live_observability_registry(
        "http://localhost:19090",
        "http://localhost:19300",
        "http://localhost:19320",
        change_reader=ControlPlaneChangeReader("http://localhost:18081/api/v1/changes").query,
    )
    records: list[dict[str, Any]] = []
    for sequence_index, sequence in enumerate(sequences, start=1):
        environment.prepare_benchmark_state(f"a1-r4-order-independence-{sequence_index}")
        lifecycle = FixtureLifecycle(environment, definitions=FIXTURE_BY_NAME)
        for scenario_id in sequence:
            scenario = by_id[scenario_id]
            environment.prepare_benchmark_state(
                f"a1-r4-order-independence-{sequence_index}-{scenario_id}"
            )
            started = time.monotonic()

            def investigate(
                incident: Any,
                alerts: tuple[Any, ...],
                fixture: str = scenario.fixture,
            ) -> Any:
                return preflight_evidence(registry, incident, alerts, fixture)

            trial, evidence = lifecycle.run(scenario, investigate=investigate)
            record = _record(scenario, trial, time.monotonic() - started)
            record.update(
                {
                    "sequence_index": sequence_index,
                    "sequence": list(sequence),
                    "evidence_count": len(evidence) if evidence is not None else 0,
                }
            )
            records.append(record)
            print(json.dumps(record, sort_keys=True), flush=True)
            if record["status"] != "PASS":
                raise RuntimeError(f"order independence failed: {sequence}/{scenario_id}")
    output = Path(os.getenv("A1_ORDER_INDEPENDENCE_OUTPUT", str(OUTPUT)))
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "artifact_type": "A1_R4_ORDER_INDEPENDENCE",
        "status": "PASS",
        "model_calls": 0,
        "sequences": [list(item) for item in sequences],
        "record_count": len(records),
        "generated_at": datetime.now(UTC).isoformat(),
        "records": records,
    }
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print("A1 order independence: PASS (0 model calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
