"""Offline tests for the canonical future live-smoke manifest contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from packages.evals.itbench.e9_identity import collect_e9_identity
from packages.evals.itbench.live_smoke_contract import build_live_smoke_manifest
from packages.evals.itbench.live_smoke_preflight import (
    LivePreflightError,
    load_manifest,
    validate_future_live_preflight,
)

ROOT = Path(__file__).resolve().parents[3]
RELEVANT_PATHS = (
    "packages/evals/itbench/e9_control.py",
    "packages/evals/itbench/e9_runtime.py",
    "packages/evals/itbench/e9_context.py",
    "packages/evals/itbench/e9_semantic.py",
    "packages/evals/itbench/e9_memory.py",
    "packages/evals/itbench/e9_packing.py",
    "packages/evals/itbench/external_contracts.py",
    "packages/provider/openai.py",
    "packages/provider/contracts.py",
    "packages/model_policy.py",
)


def _manifest_payload() -> dict[str, Any]:
    return build_live_smoke_manifest(
        ROOT, RELEVANT_PATHS, execution="ITB-CONTROL-LIVE-SMOKE-002"
    ).model_dump(mode="json")


def _preflight(
    payload: dict[str, Any], *, cap: int = 8, calls_used: int = 0, remaining: int = 8
) -> dict[str, Any]:
    return validate_future_live_preflight(
        ROOT,
        payload,
        relevant_paths=RELEVANT_PATHS,
        budget={"cap": cap, "calls_used": calls_used, "remaining": remaining},
    )


def test_generated_manifest_round_trips_and_passes_preflight() -> None:
    manifest = build_live_smoke_manifest(
        ROOT, RELEVANT_PATHS, execution="ITB-CONTROL-LIVE-SMOKE-002"
    )
    loaded = type(manifest).model_validate(manifest.model_dump(mode="json"))
    result = _preflight(loaded.model_dump(mode="json"))
    assert result["provider_constructed"] is False
    assert result["manifest"] == loaded.model_dump(mode="json")


def test_load_manifest_uses_the_canonical_typed_contract(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest_payload()), encoding="utf-8")
    manifest = load_manifest(path)
    assert manifest.execution == "ITB-CONTROL-LIVE-SMOKE-002"
    assert manifest.semantic_capability_policy_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("semantic_capability_policy_hash", None),
        ("dataset_revision", None),
        ("offline_readiness_status", None),
        ("reasoning_effort", None),
        ("required_budget", None),
    ],
)
def test_missing_canonical_fields_fail_before_provider(field: str, value: Any) -> None:
    payload = _manifest_payload()
    payload.pop(field)
    if value is not None:
        payload[field] = value
    with pytest.raises(LivePreflightError):
        _preflight(payload)


def test_historical_spelling_is_not_silently_aliased() -> None:
    payload = _manifest_payload()
    payload["semantic_availability_policy_hash"] = payload.pop("semantic_capability_policy_hash")
    with pytest.raises(LivePreflightError, match="LIVE_SMOKE_MANIFEST_INVALID"):
        _preflight(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dataset_revision", "wrong-revision", "dataset revision mismatch"),
        (
            "offline_readiness_status",
            "NOT_READY",
            "LIVE_SMOKE_MANIFEST_INVALID",
        ),
        ("reasoning_effort", "medium", "BENCHMARK_MODEL_POLICY_VIOLATION"),
        ("provider", "other", "BENCHMARK_MODEL_POLICY_VIOLATION"),
        ("provider_retries", 1, "BENCHMARK_MODEL_POLICY_VIOLATION"),
        ("judge_disabled", False, "LIVE_SMOKE_MANIFEST_INVALID"),
        ("rerun_policy", "ALLOW", "LIVE_SMOKE_MANIFEST_INVALID"),
        ("max_model_calls", 7, "future smoke limit mismatch"),
        ("max_agent_turns", 7, "future smoke limit mismatch"),
        ("max_semantic_actions", 7, "future smoke limit mismatch"),
        ("max_wall_time_seconds", 120, "future smoke limit mismatch"),
        (
            "max_consecutive_rejected_actions",
            3,
            "future smoke limit mismatch",
        ),
    ],
)
def test_invalid_manifest_values_fail_closed(field: str, value: Any, message: str) -> None:
    payload = _manifest_payload()
    payload[field] = value
    with pytest.raises(LivePreflightError, match=message):
        _preflight(payload)


def test_budget_cap_is_independent_from_model_call_limit() -> None:
    payload = _manifest_payload()
    with pytest.raises(LivePreflightError, match="fresh dedicated budget cap mismatch"):
        _preflight(payload, cap=7, remaining=7)


def test_non_fresh_budget_fails_before_provider() -> None:
    payload = _manifest_payload()
    with pytest.raises(LivePreflightError, match="live smoke budget is not fresh"):
        _preflight(payload, calls_used=1, remaining=7)


def test_source_hash_mismatch_fails_closed() -> None:
    payload = _manifest_payload()
    payload["control_policy_hash"] = "wrong"
    with pytest.raises(LivePreflightError, match="manifest content hash mismatch"):
        _preflight(payload)


def test_fixture_contract_is_synthetic_and_not_official() -> None:
    fixture = _manifest_payload()["fixture"]
    assert fixture == {
        "scenario_id": "Scenario-999",
        "fixture_path": "tests/fixtures/itbench_smoke/Scenario-999",
        "synthetic": True,
        "official_scenario": False,
        "ground_truth_loaded": False,
    }


def test_identity_builder_uses_clean_relevant_paths() -> None:
    identity = collect_e9_identity(ROOT, RELEVANT_PATHS)
    assert identity["relevant_worktree_dirty"] is False
    assert identity["git_head"]
