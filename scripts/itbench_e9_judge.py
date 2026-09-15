#!/usr/bin/env python3
"""Checkpointed, one-scenario-at-a-time Luna judging for frozen E9 outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench.judge_checkpoint import (
    SCENARIOS,
    checkpoint_is_durable,
    checkpoint_path,
)
from packages.evals.itbench.judge_policy import luna_judge_compatibility_hash
from packages.evals.itbench.persistence import atomic_json_write

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts/itbench_official_judge.py"
EVALUATOR_REVISION = "14f026fc9cc348c4ecec5ab32714de954c95c1b1"
DEFAULT_GT = ROOT / ".local/itbench-lite/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7"
DEFAULT_OUTPUTS = ROOT / ".local/itbench-lite-e9-runs/official"
DEFAULT_CHECKPOINTS = ROOT / ".local/itbench-e9-judge-runs"
DEFAULT_LEDGER = ROOT / ".local/itbench-e9-judge-budget.json"
DEFAULT_RESULT = ROOT / "docs/benchmarks/itbench-e9-official-luna-eval.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage_case(
    gt_root: Path, outputs_root: Path, scenario: str, work_root: Path
) -> tuple[Path, Path, str, str]:
    staged = Path(tempfile.mkdtemp(prefix=f"{scenario}-", dir=work_root))
    gt_dir = staged / "gt" / scenario
    output_dir = staged / "outputs" / scenario / "1" / "outputs"
    gt_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    source_gt = gt_root / scenario / "ground_truth.yaml"
    source_output = outputs_root / scenario / "1" / "outputs" / "agent_output.json"
    if not source_gt.is_file() or not source_output.is_file():
        raise RuntimeError(f"missing frozen judge input for {scenario}")
    shutil.copy2(source_gt, gt_dir / "ground_truth.yaml")
    shutil.copy2(source_output, output_dir / "agent_output.json")
    return staged / "gt", staged / "outputs", _sha256(source_gt), _sha256(source_output)


def _score(payload: dict[str, Any]) -> float:
    value = (
        payload.get("statistics", {}).get("overall", {}).get("root_cause_entity_f1", {}).get("mean")
    )
    if not isinstance(value, (int, float)):
        raise RuntimeError("judge result has no ROOT_CAUSE_ENTITY mean")
    return float(value)


def _run_one(args: argparse.Namespace, scenario: str) -> dict[str, Any]:
    checkpoint = checkpoint_path(args.checkpoint_root, scenario)
    if checkpoint_is_durable(checkpoint):
        return cast(dict[str, Any], json.loads(checkpoint.read_text(encoding="utf-8")))
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    gt_dir, outputs_dir, gt_sha, prediction_sha = _stage_case(
        args.ground_truth_root, args.outputs_root, scenario, args.checkpoint_root
    )
    result_file = checkpoint.parent / "evaluation_results.json"
    artifact = checkpoint.parent / "judge_artifact.json"
    command = [
        sys.executable,
        str(WRAPPER),
        "--ground-truth",
        str(gt_dir),
        "--outputs",
        str(outputs_dir),
        "--result-file",
        str(result_file),
        "--execution",
        "E9",
        "--expected-calls",
        "1",
        "--max-judge-calls",
        str(args.max_judge_calls),
        "--ledger",
        str(args.ledger),
        "--evaluator-cwd",
        str(args.evaluator_cwd),
        "--scenario",
        scenario,
        "--artifact",
        str(artifact),
        "--evaluator-revision",
        EVALUATOR_REVISION,
    ]
    environment = os.environ.copy()
    environment["JUDGE_MODEL"] = "gpt-5.6-luna"
    completed = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True)
    (checkpoint.parent / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    (checkpoint.parent / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"judge failed for {scenario}; inspect {artifact}")
    result = json.loads(result_file.read_text(encoding="utf-8"))
    payload = {
        "scenario": scenario,
        "evaluation_success": True,
        "root_cause_entity_f1": _score(result),
        "prediction_sha256": prediction_sha,
        "ground_truth_sha256": gt_sha,
        "judge_provider": "openai",
        "judge_model": "gpt-5.6-luna",
        "temperature": 1,
        "provider_retries": 0,
        "evaluation_attempts_per_case": 1,
        "compatibility_profile": "itbench_luna_judge_compat_v1",
        "compatibility_profile_sha256": luna_judge_compatibility_hash(),
        "evaluator_revision": EVALUATOR_REVISION,
        "return_code": completed.returncode,
    }
    atomic_json_write(checkpoint, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluator-cwd", type=Path, required=True)
    parser.add_argument("--ground-truth-root", type=Path, default=DEFAULT_GT)
    parser.add_argument("--outputs-root", type=Path, default=DEFAULT_OUTPUTS)
    parser.add_argument("--checkpoint-root", type=Path, default=DEFAULT_CHECKPOINTS)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--result-file", type=Path, default=DEFAULT_RESULT)
    parser.add_argument("--max-judge-calls", type=int, default=35)
    args = parser.parse_args()
    if args.max_judge_calls != 35:
        raise RuntimeError("E9 judge cap must equal the frozen 35-scenario schedule")
    if not args.ledger.exists():
        atomic_json_write(
            args.ledger,
            {
                "execution": "ITB-E9",
                "purpose": "JUDGE",
                "cap": 35,
                "consumed": 0,
                "remaining": 35,
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "status": "NOT_STARTED",
            },
        )
    args.checkpoint_root.mkdir(parents=True, exist_ok=True)
    results = [_run_one(args, scenario) for scenario in SCENARIOS]
    if not all(
        checkpoint_is_durable(checkpoint_path(args.checkpoint_root, scenario))
        for scenario in SCENARIOS
    ):
        raise RuntimeError("E9 judge checkpoint set is incomplete")
    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    aggregate = {
        "execution": "E9",
        "metric": "ROOT_CAUSE_ENTITY",
        "scenario_count": 35,
        "official_denominator": 35,
        "mean_f1": sum(float(item["root_cause_entity_f1"]) for item in results) / 35,
        "judge_provider": "openai",
        "judge_model": "gpt-5.6-luna",
        "compatibility_profile": "itbench_luna_judge_compat_v1",
        "temperature": 1,
        "provider_retries": 0,
        "evaluation_attempts_per_case": 1,
        "evaluator_revision": EVALUATOR_REVISION,
        "scenarios": results,
        "judge_ledger": ledger,
        "aggregation": "offline_from_atomic_scenario_checkpoints",
    }
    atomic_json_write(args.result_file, aggregate)
    print(json.dumps({"status": "ITB_E9_JUDGE_COMPLETE", "mean_f1": aggregate["mean_f1"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
