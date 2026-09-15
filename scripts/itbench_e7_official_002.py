#!/usr/bin/env python3
"""Run the frozen E7 official pass using only its dedicated agent ledger.

The frozen runner is loaded without editing its inference behavior.  This
launcher only redirects its shared ledger and keeps the historical failed
smoke charge out of the official 175-call capacity.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from packages.model_policy import validate_agent_environment
from packages.provider import LiveModelBudget

ROOT = Path(__file__).resolve().parents[1]
FROZEN_RUNNER = ROOT / "scripts/itbench_e7_execute.py"
OFFICIAL_LEDGER = ROOT / ".local/itbench-lite-e7-official-budget.json"
SOURCE_SHA = "87038086817817cdd48c94a68b3befbf48b27c60"
EXPECTED_SCENARIOS = 35


def _load_frozen_runner() -> Any:
    spec = importlib.util.spec_from_file_location("frozen_e7_official_runner", FROZEN_RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load frozen E7 runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    frozen = _load_frozen_runner()
    manifest = json.loads(frozen.MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("runtime_source_sha") != SOURCE_SHA:
        raise RuntimeError("frozen E7 runtime identity mismatch")
    validate_agent_environment()
    frozen.LEDGER = OFFICIAL_LEDGER
    dataset = frozen.ITBenchLiteDataset.open(frozen.DATA_ROOT)
    preflight = frozen.preflight(dataset, manifest)
    budget = LiveModelBudget(175, ledger_path=str(OFFICIAL_LEDGER))
    before = budget.snapshot()
    if before.calls_used != 0:
        raise RuntimeError("official E7 ledger is not fresh")
    budget.ensure_capacity(EXPECTED_SCENARIOS * frozen.LIMITS.max_model_calls)
    provider = frozen.OpenAIProvider(budget=budget, max_retry=0)
    completed = frozen.run_official(
        provider, dataset, frozen.digest(frozen.MANIFEST), str(manifest["runtime_source_sha"])
    )
    if len(completed) != EXPECTED_SCENARIOS:
        raise RuntimeError(f"expected {EXPECTED_SCENARIOS} scenarios, got {len(completed)}")
    after = budget.snapshot()
    if after.calls_used > 175:
        raise RuntimeError("official E7 ledger exceeded its preregistered cap")
    print(
        json.dumps(
            {
                "status": "ITB_E7_OFFICIAL_COMPLETE",
                "preflight": preflight,
                "scenarios": len(completed),
                "ledger": {
                    "cap": after.limit,
                    "consumed": after.calls_used,
                    "remaining": after.calls_remaining,
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
