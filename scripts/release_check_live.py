"""Validate committed live gate artifacts without making network calls."""

import json
import os
from pathlib import Path


def main() -> int:
    """Fail closed when live smoke or benchmark artifacts are incomplete."""
    smoke = json.loads(Path("docs/benchmarks/v0.2.0-live-smoke.json").read_text())
    benchmark = json.loads(Path("docs/benchmarks/v0.2.0-single-agent-live.json").read_text())
    scenarios = smoke.get("scenarios", [])
    if len(scenarios) < 3 or not all(
        item.get("termination_reason") in {"HYPOTHESIS_SUBMITTED", "AGENT_STOPPED"}
        for item in scenarios
    ):
        raise SystemExit("live smoke artifact does not prove three terminal outcomes")
    if benchmark.get("frozen_scenarios") != 10 or len(benchmark.get("scenarios", [])) != 10:
        raise SystemExit("live benchmark artifact is incomplete")
    cap = int(os.getenv("SRE_LIVE_MODEL_CALL_BUDGET", "75"))
    if benchmark.get("total_live_api_calls", cap + 1) > cap:
        raise SystemExit("live benchmark exceeded configured call cap")
    safety = benchmark.get("safety", {})
    if any(value != 0 for value in safety.values()):
        raise SystemExit("live benchmark safety artifact is not clean")
    print("live release artifacts: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
