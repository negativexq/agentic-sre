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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from packages.evals.itbench.judge_policy import (
    LUNA_JUDGE_COMPAT_PROFILE,
    JudgePlan,
    build_judge_plan,
    luna_judge_compatibility_hash,
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


def _reserve_judge_attempts(ledger: dict[str, Any], expected_calls: int) -> dict[str, Any]:
    """Reserve attempts before evaluator startup, including legacy ledgers."""
    ledger["consumed"] += expected_calls
    # Older repository-owned judge ledgers only carried the core budget
    # counters. Treat absent audit counters as zero rather than allowing a
    # pre-transport KeyError; the reservation remains the source of truth.
    ledger["provider_invocations"] = int(ledger.get("provider_invocations", 0)) + expected_calls
    ledger["outbound_attempts"] = int(ledger.get("outbound_attempts", 0)) + expected_calls
    ledger["remaining"] = ledger["cap"] - ledger["consumed"]
    ledger["status"] = "RUNNING"
    return ledger


def _prepare_luna_compatible_evaluator(source: Path) -> tuple[Path, str]:
    """Make an ephemeral compatibility copy of the pinned evaluator.

    The pinned evaluator hard-codes ``temperature=0`` and permits retry loops.
    Luna rejects that temperature, and retries would violate the project judge
    ceiling. The evaluator semantics and revision remain pinned; this copy
    changes only provider parameters and retry ceilings, and is not written
    into the repository.
    """
    source_agent = source / "itbench_evaluations" / "agent.py"
    if not source_agent.is_file():
        raise ModelPolicyError("JUDGE_EVALUATOR_INVALID", "pinned evaluator agent.py is missing")
    temporary_root = Path(tempfile.mkdtemp(prefix="itbench-luna-evaluator-"))
    destination = temporary_root / source.name
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    agent_path = destination / "itbench_evaluations" / "agent.py"
    contents = agent_path.read_text(encoding="utf-8")
    replacements = {
        "temperature=0,": "temperature=1,",
        "max_retries: int = 5": "max_retries: int = 1",
        "max_calc_retries = 3": "max_calc_retries = 1",
    }
    for old, new in replacements.items():
        if old not in contents:
            raise ModelPolicyError(
                "JUDGE_EVALUATOR_UNEXPECTED_SOURCE",
                f"compatibility anchor not found: {old}",
            )
        contents = contents.replace(old, new)
    agent_path.write_text(contents, encoding="utf-8")
    client_path = destination / "itbench_evaluations" / "client.py"
    client_contents = client_path.read_text(encoding="utf-8")
    client_anchor = "        api_key=api_key,\n"
    if client_anchor not in client_contents:
        raise ModelPolicyError(
            "JUDGE_EVALUATOR_UNEXPECTED_SOURCE",
            "OpenAI client retry anchor not found",
        )
    client_contents = client_contents.replace(
        client_anchor, "        api_key=api_key,\n        max_retries=0,\n", 1
    )
    client_path.write_text(client_contents, encoding="utf-8")
    return destination, "temperature=1; provider_max_retries=0; evaluation_attempts_per_case=1"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result_file = Path(args.result_file).resolve()
        ground_truth = Path(args.ground_truth).resolve()
        outputs = Path(args.outputs).resolve()
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
        ledger = _reserve_judge_attempts(ledger, plan.expected_calls)
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
            str(ground_truth),
            "--outputs",
            str(outputs),
            "--result-file",
            str(result_file),
            "--eval-criteria",
            "ROOT_CAUSE_ENTITY",
            "--max-concurrent",
            "1",
        ]
        evaluator_cwd, compatibility_patch = _prepare_luna_compatible_evaluator(args.evaluator_cwd)
        compatibility_hash = luna_judge_compatibility_hash()
        completed = subprocess.run(
            command,
            cwd=evaluator_cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        actual_identity_seen = (
            f"LAAJ Agent initialized with model: {identity.model}" in completed.stdout
            or f"LAAJ Agent initialized with model: {identity.model}" in completed.stderr
        )
        ledger["status"] = "COMPLETE" if completed.returncode == 0 else "FAILED"
        _atomic_json_write(plan.ledger_path, ledger)
        score: float | None = None
        result_payload: dict[str, Any] | None = None
        try:
            loaded_payload = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(loaded_payload, dict):
                result_payload = loaded_payload
            score_value = (
                (result_payload or {})
                .get("statistics", {})
                .get("overall", {})
                .get("root_cause_entity_f1", {})
                .get("mean")
            )
            if isinstance(score_value, (int, float)):
                score = float(score_value)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        successful = completed.returncode == 0 and score is not None
        if isinstance(result_payload, dict):
            # Keep the upstream evaluator payload intact while attaching the
            # project-owned, non-secret identity needed for auditability.
            result_payload["project_judge"] = {
                "provider": identity.provider,
                "model": identity.model,
                "purpose": identity.purpose,
                "judge_base_url_host": identity.base_url_host,
                "evaluator_revision": args.evaluator_revision,
                "execution": args.execution,
                "scenario": args.scenario,
                "inference_count_reserved": plan.expected_calls,
                "max_judge_calls": plan.max_calls,
                "compatibility_patch": compatibility_patch,
                "compatibility_profile": LUNA_JUDGE_COMPAT_PROFILE,
                "compatibility_profile_sha256": compatibility_hash,
            }
            result_payload["judge_ledger"] = ledger
            _atomic_json_write(result_file, result_payload)
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
                "compatibility_patch": compatibility_patch,
                "compatibility_profile": LUNA_JUDGE_COMPAT_PROFILE,
                "compatibility_profile_sha256": compatibility_hash,
                "actual_model_identity_reported": actual_identity_seen,
                "root_cause_entity_f1": score,
                "tokens": "NOT_DURABLY_AVAILABLE",
                "latency": "NOT_DURABLY_AVAILABLE",
                "return_code": completed.returncode,
                "evaluation_success": successful,
                "ledger": ledger,
            },
        )
        if completed.returncode == 0 and not actual_identity_seen:
            raise ModelPolicyError(
                "JUDGE_MODEL_IDENTITY_UNCONFIRMED",
                "evaluator did not report the approved judge model",
            )
        if not successful:
            raise ModelPolicyError(
                "JUDGE_EVALUATION_FAILED",
                "pinned evaluator did not produce a valid ROOT_CAUSE_ENTITY result",
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
