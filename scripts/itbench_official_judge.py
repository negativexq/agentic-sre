#!/usr/bin/env python3
"""Guarded launcher for the pinned ITBench evaluator.

The upstream evaluator remains untouched.  This boundary requires explicit
Luna configuration and a finite call ceiling before it can construct the
upstream process.  It is safe to use with ``--dry-run`` while credit is absent.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from packages.evals.itbench.judge_policy import (
    JudgePlan,
    build_judge_plan,
    preflight_judge,
)
from packages.model_policy import ModelExecutionIdentity, ModelPolicyError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / ".local/itbench-judge-luna-budget.json"


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    """Persist non-secret judge metadata without a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", required=True)
    parser.add_argument("--outputs", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--expected-calls", type=int, required=True)
    parser.add_argument("--max-judge-calls", type=int, required=True)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--evaluator-cwd", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--evaluator-revision", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _safe_plan_record(identity: ModelExecutionIdentity, plan: JudgePlan) -> dict[str, Any]:
    return {
        "provider": identity.provider,
        "model": identity.model,
        "purpose": identity.purpose,
        "judge_base_url_host": identity.base_url_host,
        "expected_calls": plan.expected_calls,
        "max_calls": plan.max_calls,
        "ledger": str(plan.ledger_path),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = build_judge_plan(
            expected_calls=args.expected_calls,
            max_calls=args.max_judge_calls,
            ledger_path=args.ledger,
        )
        identity, ledger = preflight_judge(plan=plan)
        print(json.dumps({"status": "PREFLIGHT_PASS", **_safe_plan_record(identity, plan)}))
        if args.dry_run:
            return 0

        # Reserve before starting the upstream process.  There is deliberately
        # no refund path: a transmitted request remains charged on failure.
        ledger["consumed"] += plan.expected_calls
        ledger["remaining"] = ledger["cap"] - ledger["consumed"]
        ledger["status"] = "RUNNING"
        _atomic_json_write(plan.ledger_path, ledger)

        env = os.environ.copy()
        # Re-state the approved value explicitly so the upstream default can
        # never be reached after our preflight.
        env["JUDGE_MODEL"] = identity.model
        command = [
            sys.executable,
            "-m",
            "itbench_evaluations",
            "--ground-truth",
            args.ground_truth,
            "--outputs",
            args.outputs,
            "--result-file",
            args.result_file,
            "--eval-criteria",
            "ROOT_CAUSE_ENTITY",
            "--max-concurrent",
            "1",
        ]
        completed = subprocess.run(command, cwd=args.evaluator_cwd, env=env, check=False)
        ledger["status"] = "COMPLETE" if completed.returncode == 0 else "FAILED"
        _atomic_json_write(plan.ledger_path, ledger)
        _atomic_json_write(
            args.artifact,
            {
                "provider": identity.provider,
                "model": identity.model,
                "purpose": identity.purpose,
                "judge_base_url_host": identity.base_url_host,
                "execution": args.execution,
                "scenario": args.scenario,
                "evaluator_revision": args.evaluator_revision,
                "inference_count_reserved": plan.expected_calls,
                "max_judge_calls": plan.max_calls,
                "return_code": completed.returncode,
                "ledger": ledger,
            },
        )
        return completed.returncode
    except ModelPolicyError as error:
        print(
            json.dumps({"status": "BLOCKED", "error_code": error.code, "error": str(error)}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
