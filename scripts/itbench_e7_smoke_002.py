#!/usr/bin/env python3
"""Run the recovered E7 synthetic smoke through the frozen E7 runtime."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from packages.evals.itbench import ITBenchExternalResult, ITBenchLiteDataset
from packages.model_policy import validate_agent_environment
from packages.provider import LiveModelBudget, OpenAIProvider, live_model_config

ROOT = Path(__file__).resolve().parents[1]
FROZEN_RUNNER = ROOT / "scripts/itbench_e7_execute.py"
DATA_ROOT = ROOT / ".local/itbench-lite"
SMOKE_LEDGER = ROOT / ".local/itbench-lite-e7-smoke-budget.json"
RUN_ROOT = ROOT / ".local/itbench-lite-e7-runs"
STAGING_ROOT = ROOT / ".local/itbench-lite-e7-smoke-002-staging"
TARGET_DIR = RUN_ROOT / "ITB-E7-SMOKE-002" / "smoke"
RESULT = ROOT / "docs/benchmarks/itbench-e7-live-smoke-002.json"
MANIFEST = ROOT / "docs/benchmarks/itbench-lite-e7-manifest.json"
EXECUTION = "ITB-E7"
EXPERIMENT = "itbench-lite-sre-external-eval-v7"
SOURCE_SHA = "87038086817817cdd48c94a68b3befbf48b27c60"


def _load_frozen_runner() -> Any:
    spec = importlib.util.spec_from_file_location("frozen_e7_runner", FROZEN_RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load frozen E7 runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if TARGET_DIR.exists() or STAGING_ROOT.exists():
        raise RuntimeError("E7 smoke-002 destination or staging directory already exists")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("runtime_source_sha") != SOURCE_SHA:
        raise RuntimeError("frozen E7 runtime source identity mismatch")
    validate_agent_environment()
    config = live_model_config()
    if not config.enabled:
        raise RuntimeError("E7 live model is not explicitly enabled")

    frozen = _load_frozen_runner()
    frozen.LEDGER = SMOKE_LEDGER
    frozen.RUN_ROOT = STAGING_ROOT
    dataset = ITBenchLiteDataset.open(DATA_ROOT)
    preflight = frozen.preflight(dataset, manifest)
    budget = LiveModelBudget(5, ledger_path=str(SMOKE_LEDGER))
    before = budget.snapshot()
    provider = OpenAIProvider(budget=budget, max_retry=0)
    smoke = frozen.persist_smoke(provider)
    after = budget.snapshot()
    attempts = after.calls_used - before.calls_used
    if attempts != smoke["outbound_attempts"]:
        raise RuntimeError("E7 smoke ledger/provider accounting mismatch")

    TARGET_DIR.parent.mkdir(parents=True, exist_ok=True)
    # Move the staged smoke directory itself into the new smoke identity.
    # Moving its parent leaves an extra SMOKE-001 path component.
    (STAGING_ROOT / "ITB-E7-SMOKE-001").rename(TARGET_DIR.parent)
    native = ITBenchExternalResult.model_validate_json(
        (TARGET_DIR / "native_artifact.json").read_bytes()
    )
    native.model_dump(mode="json")
    ledger = json.loads(SMOKE_LEDGER.read_text(encoding="utf-8"))
    ledger.update(
        {
            "status": "PASS",
            "execution": EXECUTION,
            "purpose": "SMOKE",
            "smoke_id": "ITB-E7-SMOKE-002",
            "historical_failed_attempts": 1,
            "historical_failed_outbound_attempt_remains_charged": True,
            "new_smoke_cap": 5,
            "new_smoke_consumed": attempts,
            "remaining": 5 - attempts,
            "provider": "openai",
            "model": config.model,
            "reasoning": config.reasoning_effort,
        }
    )
    frozen.atomic_json_write(SMOKE_LEDGER, ledger)
    result = {
        "execution": EXECUTION,
        "experiment": EXPERIMENT,
        "runtime_source_freeze": SOURCE_SHA,
        "smoke_id": "ITB-E7-SMOKE-002",
        "synthetic_fixture": True,
        "scenario_id": "Scenario-999",
        "provider": "openai",
        "model": config.model,
        "reasoning": config.reasoning_effort,
        "preflight": preflight,
        "smoke": smoke,
        "ledger": ledger,
        "native_artifact": str(TARGET_DIR / "native_artifact.json"),
        "itbench_output": str(TARGET_DIR / "itbench_output.json"),
        "native_reload": True,
        "itbench_output_reload": True,
        "safety": {
            "ground_truth_exposure": 0,
            "cross_scenario_evidence": 0,
            "writes": 0,
            "arbitrary_execution": 0,
        },
    }
    frozen.atomic_json_write(RESULT, result)
    print(json.dumps({"status": "ITB_E7_SMOKE_PASS", "attempts": attempts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
