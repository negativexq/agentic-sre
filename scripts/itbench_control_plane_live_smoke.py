"""Fail-closed future live-smoke preflight; this module never creates a provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packages.evals.itbench import ITBENCH_DATASET_REVISION
from packages.evals.itbench.e9_identity import collect_e9_identity, validate_e9_identity
from packages.model_policy import ModelPolicyError, validate_agent_config


class LivePreflightError(RuntimeError):
    """A future live run is blocked before provider construction."""


def validate_future_live_preflight(
    root: Path,
    manifest: dict[str, Any],
    *,
    relevant_paths: tuple[str, ...],
    budget: dict[str, Any],
) -> dict[str, Any]:
    """Validate all identity/readiness gates without importing a live provider."""
    actual = collect_e9_identity(root, relevant_paths)
    validate_e9_identity(actual, manifest)
    required_identity_fields = (
        "control_policy_hash",
        "semantic_registry_hash",
        "semantic_availability_policy_hash",
        "provider_schema_hash",
        "context_planner_hash",
    )
    missing_identity = [field for field in required_identity_fields if not manifest.get(field)]
    if missing_identity:
        raise LivePreflightError(
            "future manifest missing capability identity: " + ",".join(missing_identity)
        )
    if manifest.get("dataset_revision") != ITBENCH_DATASET_REVISION:
        raise LivePreflightError("dataset revision mismatch")
    if manifest.get("offline_readiness_status") != "READY_FOR_LIVE_SMOKE_REVIEW":
        raise LivePreflightError("offline readiness review artifact is not approved")
    try:
        identity = validate_agent_config(str(manifest.get("model")), str(manifest.get("reasoning")))
    except ModelPolicyError as error:
        raise LivePreflightError(error.code) from error
    if budget.get("cap") != int(manifest.get("required_budget", 0)):
        raise LivePreflightError("fresh dedicated budget cap mismatch")
    if int(budget.get("calls_used", -1)) != 0 or int(budget.get("remaining", -1)) != int(
        budget["cap"]
    ):
        raise LivePreflightError("live smoke budget is not fresh")
    return {
        "runtime_identity": actual,
        "model_identity": identity.as_dict(),
        "provider_constructed": False,
    }


def load_manifest(path: Path) -> dict[str, Any]:
    """Strictly load a future manifest for callers; no execution occurs."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LivePreflightError("manifest must be an object")
    return value


__all__ = ["LivePreflightError", "load_manifest", "validate_future_live_preflight"]
