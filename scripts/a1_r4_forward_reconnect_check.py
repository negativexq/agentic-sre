"""Verify supervised observability forwards survive benchmark resets."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from packages.evals import LiveBenchmarkEnvironment

OUTPUT = Path("docs/benchmarks/a1-r4-forward-reconnect.json")
REPEATS = 5


def main() -> int:
    environment = LiveBenchmarkEnvironment()
    records: list[dict[str, object]] = []
    for cycle in range(1, REPEATS + 1):
        started = time.monotonic()
        environment.prepare_benchmark_state(f"a1-r4-forward-reconnect-{cycle}")
        prometheus = environment._wait_for_endpoint_ready(
            f"{environment.prometheus_url}/-/ready", timeout_seconds=60
        )
        alertmanager = environment._wait_for_endpoint_ready(
            f"{environment.alertmanager_url}/-/ready", timeout_seconds=60
        )
        baseline = environment.baseline_oracle()
        passed = prometheus and alertmanager and baseline.passed
        record = {
            "cycle": cycle,
            "prometheus_reconnect": prometheus,
            "alertmanager_reconnect": alertmanager,
            "readiness_seconds": max(time.monotonic() - started, 0.0),
            "baseline": baseline.model_dump(mode="json"),
            "status": "PASS" if passed else "FAIL",
        }
        records.append(record)
        print(json.dumps(record, sort_keys=True), flush=True)
        if not passed:
            raise RuntimeError(f"forward reconnect failed at cycle {cycle}")
    report = {
        "artifact_type": "A1_R4_FORWARD_RECONNECT_QUALIFICATION",
        "repeat_count": REPEATS,
        "pass_count": len(records),
        "record_count": len(records),
        "openai_calls": 0,
        "generated_at": datetime.now(UTC).isoformat(),
        "records": records,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
