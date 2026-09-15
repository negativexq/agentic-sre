#!/usr/bin/env python3
"""Checkpointed, one-scenario-at-a-time Luna judging for frozen E8 outputs."""

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
from tempfile import NamedTemporaryFile
from typing import Any, cast

from packages.evals.itbench.judge_checkpoint import (
    SCENARIOS,
    checkpoint_is_durable,
    checkpoint_path,
)

ROOT = Path(__file__).resolve().parents[1]
JUDGE_WRAPPER = ROOT / "scripts/itbench_official_judge.py"
DEFAULT_LEDGER = ROOT / ".local/itbench-e8-judge-budget.json"
DEFAULT_CHECKPOINT_ROOT = ROOT / ".local/itbench-e8-judge-runs"
EVALUATOR_REVISION = "14f026fc9cc348c4ecec5ab32714de954c95c1b1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _stage_case(
    *, ground_truth_root: Path, outputs_root: Path, scenario: str, work_root: Path
) -> tuple[Path, Path, str, str]:
    """Stage exactly one frozen prediction and GT case for upstream loading."""
    staged = Path(tempfile.mkdtemp(prefix=f"{scenario}-", dir=work_root))
    gt_dir = staged / "gt" / scenario
    output_dir = staged / "outputs" / scenario / "1" / "outputs"
    gt_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    source_gt = ground_truth_root / scenario / "ground_truth.yaml"
    source_output = outputs_root / scenario / "1" / "outputs" / "agent_output.json"
    if not source_gt.is_file() or not source_output.is_file():
        raise RuntimeError(f"missing frozen judge input for {scenario}")
    shutil.copy2(source_gt, gt_dir / "ground_truth.yaml")
    shutil.copy2(source_output, output_dir / "agent_output.json")
    return staged / "gt", staged / "outputs", _sha256(source_gt), _sha256(source_output)


def _score_from_result(payload: dict[str, Any]) -> float:
    value = (
        payload.get("statistics", {}).get("overall", {}).get("root_cause_entity_f1", {}).get("mean")
    )
    if not isinstance(value, (int, float)):
        raise RuntimeError("judge result has no ROOT_CAUSE_ENTITY mean")
    return float(value)


def _run_one(*, scenario: str, args: argparse.Namespace, checkpoint_root: Path) -> dict[str, Any]:
    path = checkpoint_path(checkpoint_root, scenario)
    if checkpoint_is_durable(path):
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    path.parent.mkdir(parents=True, exist_ok=True)
    gt_dir, outputs_dir, gt_sha, prediction_sha = _stage_case(
        ground_truth_root=args.ground_truth_root,
        outputs_root=args.outputs_root,
        scenario=scenario,
        work_root=checkpoint_root,
    )
    scenario_root = path.parent
    result_file = scenario_root / "evaluation_results.json"
    judge_artifact = scenario_root / "judge_artifact.json"
    command = [
        sys.executable,
        str(JUDGE_WRAPPER),
        "--ground-truth",
        str(gt_dir),
        "--outputs",
        str(outputs_dir),
        "--result-file",
        str(result_file),
        "--execution",
        "E8",
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
        str(judge_artifact),
        "--evaluator-revision",
        EVALUATOR_REVISION,
    ]
    environment = os.environ.copy()
    environment["JUDGE_MODEL"] = "gpt-5.6-luna"
    completed = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True)
    (scenario_root / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    (scenario_root / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"judge failed for {scenario}; inspect {judge_artifact}")
    result = json.loads(result_file.read_text(encoding="utf-8"))
    checkpoint = {
        "scenario": scenario,
        "evaluation_success": True,
        "root_cause_entity_f1": _score_from_result(result),
        "prediction_sha256": prediction_sha,
        "ground_truth_sha256": gt_sha,
        "judge_provider": "openai",
        "judge_model": "gpt-5.6-luna",
        "temperature": 1,
        "provider_retries": 0,
        "evaluation_attempts_per_case": 1,
        "compatibility_profile": "itbench_luna_judge_compat_v1",
        "evaluator_revision": EVALUATOR_REVISION,
        "return_code": completed.returncode,
    }
    _atomic_json_write(path, checkpoint)
    return checkpoint


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth-root", type=Path, required=True)
    parser.add_argument("--outputs-root", type=Path, required=True)
    parser.add_argument("--evaluator-cwd", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--checkpoint-root", type=Path, default=DEFAULT_CHECKPOINT_ROOT)
    parser.add_argument("--result-file", type=Path, required=True)
    parser.add_argument("--max-judge-calls", type=int, default=35)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.max_judge_calls != len(SCENARIOS):
        raise RuntimeError("E8 judge cap must equal the frozen 35-scenario schedule")
    args.checkpoint_root.mkdir(parents=True, exist_ok=True)
    results = []
    for scenario in SCENARIOS:
        results.append(_run_one(scenario=scenario, args=args, checkpoint_root=args.checkpoint_root))
        print(f"checkpointed {scenario}", flush=True)
    if len(results) != len(SCENARIOS) or any(
        not checkpoint_is_durable(checkpoint_path(args.checkpoint_root, item)) for item in SCENARIOS
    ):
        raise RuntimeError("E8 judge checkpoint set is incomplete")
    aggregate = {
        "execution": "E8",
        "metric": "ROOT_CAUSE_ENTITY",
        "scenario_count": len(results),
        "official_denominator": len(results),
        "mean_f1": sum(float(item["root_cause_entity_f1"]) for item in results) / len(results),
        "judge_provider": "openai",
        "judge_model": "gpt-5.6-luna",
        "compatibility_profile": "itbench_luna_judge_compat_v1",
        "temperature": 1,
        "provider_retries": 0,
        "evaluation_attempts_per_case": 1,
        "evaluator_revision": EVALUATOR_REVISION,
        "scenarios": results,
        "judge_ledger": json.loads(args.ledger.read_text(encoding="utf-8")),
        "aggregation": "offline_from_atomic_scenario_checkpoints",
    }
    _atomic_json_write(args.result_file, aggregate)
    print(json.dumps({"status": "ITB_E8_JUDGE_COMPLETE", "mean_f1": aggregate["mean_f1"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
