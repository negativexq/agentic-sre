"""Offline-safe planning and preflight for the pinned ITBench judge."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.model_policy import (
    ModelExecutionIdentity,
    ModelPolicyError,
    validate_judge_environment,
)

LUNA_JUDGE_COMPAT_PROFILE: dict[str, object] = {
    "name": "itbench_luna_judge_compat_v1",
    "version": 1,
    "provider": "openai",
    "model": "gpt-5.6-luna",
    "temperature": 1,
    "provider_max_retries": 0,
    "evaluation_attempts_per_case": 1,
    "fallback_model": "NONE",
    "fallback_temperature": "NONE",
}


def luna_judge_compatibility_hash() -> str:
    """Return the stable identity of the Luna-only provider compatibility profile."""
    encoded = json.dumps(LUNA_JUDGE_COMPAT_PROFILE, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class JudgePlan:
    """A bounded, preflighted evaluator operation."""

    expected_calls: int
    max_calls: int
    ledger_path: Path


def build_judge_plan(*, expected_calls: int, max_calls: int, ledger_path: Path) -> JudgePlan:
    if expected_calls <= 0 or max_calls <= 0:
        raise ModelPolicyError("JUDGE_CALL_CAP_INVALID", "judge call ceilings must be positive")
    if expected_calls > max_calls:
        raise ModelPolicyError(
            "JUDGE_CALL_CAP_EXCEEDED",
            "expected evaluator work exceeds the explicit judge call cap",
        )
    return JudgePlan(expected_calls, max_calls, ledger_path)


def read_judge_ledger(path: Path) -> dict[str, Any]:
    """Load and reconcile the separate judge ledger without contacting a provider."""
    if not path.exists():
        raise ModelPolicyError("JUDGE_LEDGER_MISSING", "judge ledger does not exist")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelPolicyError("JUDGE_LEDGER_INVALID", "judge ledger is invalid") from error
    if not isinstance(value, dict):
        raise ModelPolicyError("JUDGE_LEDGER_INVALID", "judge ledger must be an object")
    for key in ("cap", "consumed", "remaining"):
        if not isinstance(value.get(key), int) or value[key] < 0:
            raise ModelPolicyError("JUDGE_LEDGER_INVALID", "judge ledger counters are invalid")
    if value["remaining"] != value["cap"] - value["consumed"]:
        raise ModelPolicyError("JUDGE_LEDGER_INVALID", "judge ledger does not reconcile")
    return value


def preflight_judge(
    *, plan: JudgePlan, environ: dict[str, str] | None = None, require_credentials: bool = True
) -> tuple[ModelExecutionIdentity, dict[str, Any]]:
    """Validate model/provider identity and capacity before evaluator startup."""
    identity = validate_judge_environment(environ, require_credentials=require_credentials)
    ledger = read_judge_ledger(plan.ledger_path)
    if ledger["consumed"] + plan.expected_calls > ledger["cap"]:
        raise ModelPolicyError(
            "JUDGE_LEDGER_CAP_EXCEEDED",
            "judge ledger cannot cover the requested evaluator work",
        )
    return identity, ledger


__all__ = [
    "LUNA_JUDGE_COMPAT_PROFILE",
    "JudgePlan",
    "build_judge_plan",
    "luna_judge_compatibility_hash",
    "preflight_judge",
    "read_judge_ledger",
]
