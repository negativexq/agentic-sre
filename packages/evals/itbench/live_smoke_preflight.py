"""Fail-closed future live-smoke preflight implementation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from packages.evals.itbench.dataset import ITBENCH_DATASET_REVISION
from packages.evals.itbench.e9_identity import collect_e9_identity, validate_e9_identity
from packages.evals.itbench.e9_runtime import ITBENCH_E9_PROMPT_VERSION, e9_prompt_hash
from packages.evals.itbench.external_contracts import ITBENCH_EXTERNAL_PROTOCOL_V5
from packages.evals.itbench.live_smoke_contract import (
    LIVE_SMOKE_MAX_AGENT_TURNS,
    LIVE_SMOKE_MAX_CONSECUTIVE_REJECTIONS,
    LIVE_SMOKE_MAX_MODEL_CALLS,
    LIVE_SMOKE_MAX_SEMANTIC_ACTIONS,
    LIVE_SMOKE_MAX_WALL_TIME_SECONDS,
    LIVE_SMOKE_OFFLINE_READINESS,
    ITBenchLiveSmokeManifestV1,
)
from packages.model_policy import ModelPolicyError, validate_agent_config


class LivePreflightError(RuntimeError):
    """A future live run is blocked before provider construction."""


def _typed_manifest(
    manifest: ITBenchLiveSmokeManifestV1 | dict[str, Any],
) -> ITBenchLiveSmokeManifestV1:
    if isinstance(manifest, ITBenchLiveSmokeManifestV1):
        return manifest
    try:
        return ITBenchLiveSmokeManifestV1.model_validate(manifest)
    except ValidationError as error:
        locations = {
            ".".join(str(item) for item in detail.get("loc", ())) for detail in error.errors()
        }
        policy_fields = {"provider", "model", "reasoning_effort", "provider_retries"}
        code = (
            "BENCHMARK_MODEL_POLICY_VIOLATION"
            if locations.intersection(policy_fields)
            else "LIVE_SMOKE_MANIFEST_INVALID"
        )
        raise LivePreflightError(code) from error


def validate_future_live_preflight(
    root: Path,
    manifest: ITBenchLiveSmokeManifestV1 | dict[str, Any],
    *,
    relevant_paths: tuple[str, ...],
    budget: dict[str, Any],
) -> dict[str, Any]:
    """Validate all identity/readiness gates without importing a live provider."""
    typed_manifest = _typed_manifest(manifest)
    actual = collect_e9_identity(root, relevant_paths)
    validate_e9_identity(
        actual,
        {"runtime_identity": typed_manifest.runtime_identity.model_dump(mode="json")},
    )
    required_identity_fields = (
        "control_policy_hash",
        "semantic_registry_hash",
        "semantic_capability_policy_hash",
        "provider_schema_hash",
        "context_planner_hash",
    )
    missing_identity = [
        field for field in required_identity_fields if not getattr(typed_manifest, field, None)
    ]
    if missing_identity:
        raise LivePreflightError(
            "future manifest missing capability identity: " + ",".join(missing_identity)
        )
    expected_source_hashes = {
        "control_policy_hash": "packages/evals/itbench/e9_control.py",
        "semantic_registry_hash": "packages/evals/itbench/e9_semantic.py",
        "semantic_capability_policy_hash": "packages/evals/itbench/e9_semantic.py",
        "provider_schema_hash": "packages/provider/openai.py",
        "context_planner_hash": "packages/evals/itbench/e9_context.py",
        "candidate_discovery_hash": "packages/evals/itbench/e9_semantic.py",
        "protocol_hash": "packages/evals/itbench/external_contracts.py",
    }
    for field, path in expected_source_hashes.items():
        if getattr(typed_manifest, field) != actual["relevant_content_sha256"].get(path):
            raise LivePreflightError(f"manifest content hash mismatch: {field}")
    if typed_manifest.prompt_version != ITBENCH_E9_PROMPT_VERSION:
        raise LivePreflightError("prompt version mismatch")
    if typed_manifest.prompt_hash != e9_prompt_hash():
        raise LivePreflightError("prompt hash mismatch")
    if typed_manifest.protocol_version != ITBENCH_EXTERNAL_PROTOCOL_V5:
        raise LivePreflightError("protocol version mismatch")
    if typed_manifest.dataset_revision != ITBENCH_DATASET_REVISION:
        raise LivePreflightError("dataset revision mismatch")
    if typed_manifest.offline_readiness_status != LIVE_SMOKE_OFFLINE_READINESS:
        raise LivePreflightError("offline readiness review artifact is not approved")
    try:
        identity = validate_agent_config(typed_manifest.model, typed_manifest.reasoning_effort)
    except ModelPolicyError as error:
        raise LivePreflightError(error.code) from error
    if typed_manifest.provider != "openai":
        raise LivePreflightError("provider policy violation")
    if typed_manifest.provider_retries != 0:
        raise LivePreflightError("provider retries must be zero")
    if typed_manifest.judge_disabled is not True:
        raise LivePreflightError("judge must be disabled")
    if typed_manifest.rerun_policy != "NEVER":
        raise LivePreflightError("live smoke reruns are forbidden")
    expected_limits = {
        "max_model_calls": LIVE_SMOKE_MAX_MODEL_CALLS,
        "max_agent_turns": LIVE_SMOKE_MAX_AGENT_TURNS,
        "max_semantic_actions": LIVE_SMOKE_MAX_SEMANTIC_ACTIONS,
        "max_wall_time_seconds": LIVE_SMOKE_MAX_WALL_TIME_SECONDS,
        "max_consecutive_rejected_actions": LIVE_SMOKE_MAX_CONSECUTIVE_REJECTIONS,
    }
    for field, expected in expected_limits.items():
        if getattr(typed_manifest, field) != expected:
            raise LivePreflightError(f"future smoke limit mismatch: {field}")
    fixture = typed_manifest.fixture
    if fixture.fixture_path != "tests/fixtures/itbench_smoke/Scenario-999":
        raise LivePreflightError("future smoke fixture mismatch")
    if not (root / fixture.fixture_path).is_dir():
        raise LivePreflightError("future smoke fixture is missing")
    if budget.get("cap") != typed_manifest.required_budget:
        raise LivePreflightError("fresh dedicated budget cap mismatch")
    if int(budget.get("calls_used", -1)) != 0 or int(budget.get("remaining", -1)) != int(
        budget["cap"]
    ):
        raise LivePreflightError("live smoke budget is not fresh")
    return {
        "runtime_identity": actual,
        "model_identity": identity.as_dict(),
        "manifest": typed_manifest.model_dump(mode="json"),
        "runtime_limits": {
            "max_model_calls": typed_manifest.max_model_calls,
            "max_agent_turns": typed_manifest.max_agent_turns,
            "max_semantic_actions": typed_manifest.max_semantic_actions,
            "max_wall_time_seconds": typed_manifest.max_wall_time_seconds,
            "max_consecutive_rejected_actions": typed_manifest.max_consecutive_rejected_actions,
            "provider_retries": typed_manifest.provider_retries,
        },
        "provider_constructed": False,
    }


def load_manifest(path: Path) -> ITBenchLiveSmokeManifestV1:
    """Strictly load a future manifest for callers; no execution occurs."""
    value = json.loads(path.read_text(encoding="utf-8"))
    return _typed_manifest(value)


__all__ = ["LivePreflightError", "load_manifest", "validate_future_live_preflight"]
