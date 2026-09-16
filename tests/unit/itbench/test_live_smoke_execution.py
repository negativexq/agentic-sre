"""Offline tests for the manifest-bound future live-smoke executor."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from packages.evals.itbench.e9_identity import E9IdentityMismatch, collect_e9_identity
from packages.evals.itbench.e9_runtime import E9Limits
from packages.evals.itbench.live_smoke_contract import (
    LIVE_SMOKE_RELEVANT_PATHS,
    ITBenchLiveSmokeManifestV1,
    build_live_smoke_manifest,
)
from packages.evals.itbench.live_smoke_execution import (
    LiveSmokeExecutionError,
    _limits_from_manifest,
    _live_provider_factory,
    execute_live_smoke,
)
from packages.evals.itbench.live_smoke_preflight import LivePreflightError
from packages.provider import FakeModelProvider, LiveModelBudget

ROOT = Path(__file__).resolve().parents[3]
RELEVANT_PATHS = LIVE_SMOKE_RELEVANT_PATHS


def _manifest_payload() -> dict[str, Any]:
    return build_live_smoke_manifest(
        ROOT, RELEVANT_PATHS, execution="ITB-CONTROL-LIVE-SMOKE-002"
    ).model_dump(mode="json")


def _write_inputs(tmp_path: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    manifest_path = tmp_path / "manifest.json"
    ledger_path = tmp_path / "budget.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    ledger_path.write_text(
        json.dumps(
            {
                "execution": "ITB-CONTROL-LIVE-SMOKE-002",
                "purpose": "AGENT",
                "cap": 8,
                "calls_used": 0,
                "remaining": 8,
                "status": "NOT_STARTED",
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, ledger_path


def _fake_factory(
    seen: dict[str, Any],
) -> Callable[[ITBenchLiveSmokeManifestV1, LiveModelBudget], FakeModelProvider]:
    def factory(manifest: ITBenchLiveSmokeManifestV1, budget: LiveModelBudget) -> FakeModelProvider:
        seen.update(
            {
                "execution": manifest.execution,
                "budget_limit": budget.snapshot().limit,
                "provider_retries": manifest.provider_retries,
            }
        )
        return FakeModelProvider(
            [
                {"action": "HYPOTHESIZE", "target": "C001", "rationale": "inspect seed"},
                {
                    "action": "INVESTIGATE",
                    "target": "C001",
                    "operation": "ENTITY_CONTEXT",
                    "rationale": "collect evidence",
                },
                {"action": "STOP", "stop_reason": "investigation_complete"},
            ]
        )

    return factory


def test_manifest_limits_are_bound_to_runtime() -> None:
    manifest = build_live_smoke_manifest(ROOT, RELEVANT_PATHS)
    assert _limits_from_manifest(manifest) == E9Limits(
        max_model_calls=8,
        max_tool_calls=8,
        max_agent_turns=8,
        max_wall_time_seconds=180,
    )


def test_fake_end_to_end_persists_and_reloads_without_gt_or_judge(tmp_path: Path) -> None:
    manifest_path, ledger_path = _write_inputs(tmp_path, _manifest_payload())
    result_path = tmp_path / "result.json"
    run_dir = tmp_path / "run" / "Scenario-999"
    seen: dict[str, Any] = {}

    result = execute_live_smoke(
        ROOT,
        manifest_path,
        ledger_path,
        result_path,
        run_dir,
        relevant_paths=RELEVANT_PATHS,
        provider_factory=_fake_factory(seen),
    )

    assert result["terminal"] == "STOP"
    assert result["metrics"]["model_calls"] == 3
    assert result["metrics"]["evidence_count"] >= 1
    assert seen == {
        "execution": "ITB-CONTROL-LIVE-SMOKE-002",
        "budget_limit": 8,
        "provider_retries": 0,
    }
    assert result["safety"] == {
        "provider_fallback": 0,
        "ground_truth_access": 0,
        "cross_scenario_access": 0,
        "writes_remediation": 0,
        "shell": 0,
        "sql": 0,
        "arbitrary_promql": 0,
        "judge_evaluator_access": 0,
    }
    assert result_path.is_file()
    assert (run_dir / "native_artifact.json").is_file()
    assert (run_dir / "event_log.json").is_file()
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["status"] == "COMPLETE"
    assert ledger["calls_used"] == 0
    assert ledger["remaining"] == 8


def test_preflight_failure_does_not_invoke_provider_factory(tmp_path: Path) -> None:
    payload = _manifest_payload()
    payload["dataset_revision"] = "wrong-revision"
    manifest_path, ledger_path = _write_inputs(tmp_path, payload)
    called = False

    def factory(_manifest: Any, _budget: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("provider factory must not run")

    with pytest.raises(Exception, match="dataset revision mismatch"):
        execute_live_smoke(
            ROOT,
            manifest_path,
            ledger_path,
            tmp_path / "result.json",
            tmp_path / "run",
            relevant_paths=RELEVANT_PATHS,
            provider_factory=factory,
        )
    assert called is False
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["status"] == "NOT_STARTED"
    assert ledger["calls_used"] == 0


@pytest.mark.parametrize(
    "failure",
    [
        "runtime_identity",
        "dirty_paths",
        "manifest_hash",
        "dataset_revision",
        "model_policy",
        "provider_retries",
        "budget_cap",
        "nonfresh_budget",
        "fixture",
        "runtime_limit",
    ],
)
def test_every_preflight_failure_blocks_provider_factory(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _manifest_payload()
    if failure == "runtime_identity":
        payload["runtime_identity"]["git_head"] = "not-the-frozen-head"
    elif failure == "manifest_hash":
        payload["control_policy_hash"] = "wrong"
    elif failure == "dataset_revision":
        payload["dataset_revision"] = "wrong"
    elif failure == "model_policy":
        payload["model"] = "gpt-5.6-sol"
    elif failure == "provider_retries":
        payload["provider_retries"] = 1
    elif failure == "fixture":
        payload["fixture"]["fixture_path"] = "tests/fixtures/other"
    elif failure == "runtime_limit":
        payload["max_agent_turns"] = 7
    manifest_path, ledger_path = _write_inputs(tmp_path, payload)
    if failure == "budget_cap":
        ledger_path.write_text(
            ledger_path.read_text(encoding="utf-8")
            .replace('"cap": 8', '"cap": 7')
            .replace('"remaining": 8', '"remaining": 7'),
            encoding="utf-8",
        )
    elif failure == "nonfresh_budget":
        ledger_path.write_text(
            ledger_path.read_text(encoding="utf-8")
            .replace('"calls_used": 0', '"calls_used": 1')
            .replace('"remaining": 8', '"remaining": 7'),
            encoding="utf-8",
        )
    elif failure == "dirty_paths":
        import packages.evals.itbench.live_smoke_preflight as preflight

        actual = collect_e9_identity(ROOT, RELEVANT_PATHS)
        actual["relevant_worktree_dirty"] = True

        def dirty_identity(_root: Path, _paths: tuple[str, ...]) -> dict[str, Any]:
            return actual

        monkeypatch.setattr(preflight, "collect_e9_identity", dirty_identity)

    called = False

    def factory(_manifest: Any, _budget: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("provider factory must not run")

    with pytest.raises((E9IdentityMismatch, LivePreflightError, LiveSmokeExecutionError)):
        execute_live_smoke(
            ROOT,
            manifest_path,
            ledger_path,
            tmp_path / "result.json",
            tmp_path / "run",
            relevant_paths=RELEVANT_PATHS,
            provider_factory=factory,
        )
    assert called is False


def test_existing_result_or_run_refuses_rerun(tmp_path: Path) -> None:
    manifest_path, ledger_path = _write_inputs(tmp_path, _manifest_payload())
    result_path = tmp_path / "result.json"
    result_path.write_text("{}", encoding="utf-8")
    with pytest.raises(LiveSmokeExecutionError, match="rerun refused"):
        execute_live_smoke(
            ROOT,
            manifest_path,
            ledger_path,
            result_path,
            tmp_path / "run",
            relevant_paths=RELEVANT_PATHS,
            provider_factory=_fake_factory({}),
        )


def test_live_provider_factory_binds_zero_retry_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import packages.evals.itbench.live_smoke_execution as execution

    manifest = build_live_smoke_manifest(ROOT, RELEVANT_PATHS)
    budget = LiveModelBudget(8, ledger_path=str(tmp_path / "budget.json"))
    observed: dict[str, Any] = {}

    class ProviderStub:
        def __init__(self, *, budget: LiveModelBudget, config: Any, max_retry: int) -> None:
            observed.update(
                {
                    "budget": budget.snapshot().limit,
                    "model": config.model,
                    "reasoning_effort": config.reasoning_effort,
                    "enabled": config.enabled,
                    "max_retry": max_retry,
                }
            )

    monkeypatch.setenv("SRE_LIVE_MODEL_ENABLED", "true")
    monkeypatch.setenv("SRE_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("SRE_REASONING_EFFORT", "none")
    monkeypatch.setattr(execution, "OpenAIProvider", ProviderStub)

    _live_provider_factory(manifest, budget)

    assert observed == {
        "budget": 8,
        "model": "gpt-5.6-luna",
        "reasoning_effort": "none",
        "enabled": True,
        "max_retry": 0,
    }
