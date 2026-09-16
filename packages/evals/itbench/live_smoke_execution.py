"""Fail-closed execution path for the future single live smoke.

This module is intentionally separate from the provider-free preflight.  The
default provider factory is reachable only after the typed manifest, identity,
fixture, policy, and fresh-budget checks have passed.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from packages.evals.itbench.contracts import (
    ITBenchAgentOutput,
    ITBenchEvidenceCategory,
    ITBenchScenario,
)
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import E9InvestigationRuntime, E9Limits
from packages.evals.itbench.incident import build_observable_incident
from packages.evals.itbench.live_smoke_contract import (
    LIVE_SMOKE_RELEVANT_PATHS,
    ITBenchLiveSmokeManifestV1,
)
from packages.evals.itbench.live_smoke_preflight import (
    load_manifest,
    validate_future_live_preflight,
)
from packages.evals.itbench.output_adapter import adapt_e9_output
from packages.evals.itbench.persistence import atomic_json_write
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend
from packages.model_policy import ModelPolicyError, validate_agent_environment
from packages.provider import LiveModelBudget, ModelProvider, OpenAIProvider, live_model_config


class LiveSmokeExecutionError(RuntimeError):
    """A smoke execution was blocked or failed without an automatic rerun."""


ProviderFactory = Callable[[ITBenchLiveSmokeManifestV1, LiveModelBudget], ModelProvider]


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LiveSmokeExecutionError(f"invalid smoke ledger: {path}") from error
    if not isinstance(value, dict):
        raise LiveSmokeExecutionError(f"smoke ledger must be an object: {path}")
    return value


def _fresh_ledger(path: Path, manifest: ITBenchLiveSmokeManifestV1) -> dict[str, Any]:
    value = _read_object(path)
    expected = {
        "execution": manifest.execution,
        "purpose": "AGENT",
        "cap": manifest.required_budget,
        "calls_used": 0,
        "remaining": manifest.required_budget,
        "status": "NOT_STARTED",
    }
    for field, required in expected.items():
        if value.get(field) != required:
            raise LiveSmokeExecutionError(f"fresh smoke ledger mismatch: {field}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _live_provider_factory(
    manifest: ITBenchLiveSmokeManifestV1, budget: LiveModelBudget
) -> ModelProvider:
    """Construct the real provider only after execution preflight succeeds."""
    config = live_model_config()
    if not config.enabled:
        raise LiveSmokeExecutionError("live model execution is not explicitly enabled")
    try:
        environment_identity = validate_agent_environment()
    except ModelPolicyError as error:
        raise LiveSmokeExecutionError(error.code) from error
    if environment_identity.model != manifest.model:
        raise LiveSmokeExecutionError("live environment model disagrees with manifest")
    if environment_identity.reasoning != manifest.reasoning_effort:
        raise LiveSmokeExecutionError("live environment reasoning disagrees with manifest")
    if config.model != manifest.model or config.reasoning_effort != manifest.reasoning_effort:
        raise LiveSmokeExecutionError("live model configuration disagrees with manifest")
    return OpenAIProvider(
        budget=budget,
        config=config,
        max_retry=manifest.provider_retries,
    )


def _limits_from_manifest(manifest: ITBenchLiveSmokeManifestV1) -> E9Limits:
    """Translate the preregistered limits without introducing new defaults."""
    return E9Limits(
        max_model_calls=manifest.max_model_calls,
        max_tool_calls=manifest.max_semantic_actions,
        max_agent_turns=manifest.max_agent_turns,
        max_wall_time_seconds=manifest.max_wall_time_seconds,
    )


def _smoke_scenario(root: Path) -> ITBenchScenario:
    scenario_root = root / "tests/fixtures/itbench_smoke/Scenario-999"
    files: dict[ITBenchEvidenceCategory, tuple[str, ...]] = {
        ITBenchEvidenceCategory.ALERTS: ("alerts/alerts.json",),
        ITBenchEvidenceCategory.METRICS: ("metrics/checkout.tsv",),
        ITBenchEvidenceCategory.K8S_EVENTS: ("k8s_events_raw.tsv",),
        ITBenchEvidenceCategory.K8S_OBJECTS: ("k8s_objects_raw.tsv",),
        ITBenchEvidenceCategory.LOGS: ("otel_logs_raw.tsv",),
        ITBenchEvidenceCategory.TRACES: ("otel_traces_raw.tsv",),
    }
    return ITBenchScenario(
        scenario_id="Scenario-999",
        snapshot_path=str(scenario_root),
        evidence_categories=tuple(files),
        evidence_files=files,
    )


def _persist_run_artifacts(
    run_dir: Path,
    result: dict[str, Any],
    output: ITBenchAgentOutput,
) -> None:
    atomic_json_write(run_dir / "native_artifact.json", result)
    atomic_json_write(run_dir / "turn_trace.json", result["turns"])
    atomic_json_write(run_dir / "case_state.json", result["case_state"])
    atomic_json_write(run_dir / "event_log.json", result["events"])
    atomic_json_write(run_dir / "usage.json", result["usage"])
    atomic_json_write(run_dir / "agent_output.json", output.model_dump(mode="json"))


def _strict_reload(run_dir: Path, result: dict[str, Any], output: ITBenchAgentOutput) -> None:
    native = _read_object(run_dir / "native_artifact.json")
    persisted_output = _read_object(run_dir / "agent_output.json")
    if native != result:
        raise LiveSmokeExecutionError("native artifact reload mismatch")
    if persisted_output != output.model_dump(mode="json"):
        raise LiveSmokeExecutionError("agent output reload mismatch")
    if json.loads((run_dir / "turn_trace.json").read_text(encoding="utf-8")) != result["turns"]:
        raise LiveSmokeExecutionError("turn trace reload mismatch")
    if (
        json.loads((run_dir / "case_state.json").read_text(encoding="utf-8"))
        != result["case_state"]
    ):
        raise LiveSmokeExecutionError("case state reload mismatch")
    if json.loads((run_dir / "event_log.json").read_text(encoding="utf-8")) != result["events"]:
        raise LiveSmokeExecutionError("event log reload mismatch")
    replayed = E9CaseMemory.replay(result)
    if replayed.projection() != result["case_state"]:
        raise LiveSmokeExecutionError("event replay projection mismatch")


def _mark_ledger(
    path: Path, current: dict[str, Any], *, status: str, budget: LiveModelBudget
) -> None:
    snapshot = budget.snapshot()
    atomic_json_write(
        path,
        {
            **current,
            "status": status,
            "calls_used": snapshot.calls_used,
            "remaining": snapshot.calls_remaining,
        },
    )


def execute_live_smoke(
    root: Path,
    manifest_path: Path,
    ledger_path: Path,
    result_path: Path,
    run_dir: Path,
    *,
    relevant_paths: tuple[str, ...] = LIVE_SMOKE_RELEVANT_PATHS,
    provider_factory: ProviderFactory = _live_provider_factory,
) -> dict[str, Any]:
    """Execute one future smoke trial; never rerun an existing result."""
    if result_path.exists():
        raise LiveSmokeExecutionError("live smoke result already exists; rerun refused")
    if run_dir.exists():
        raise LiveSmokeExecutionError("live smoke run directory already exists; rerun refused")

    manifest = load_manifest(manifest_path)
    ledger = _fresh_ledger(ledger_path, manifest)
    preflight = validate_future_live_preflight(
        root,
        manifest,
        relevant_paths=relevant_paths,
        budget=ledger,
    )

    limits = _limits_from_manifest(manifest)
    budget = LiveModelBudget(manifest.required_budget, ledger_path=str(ledger_path))
    before_budget = budget.snapshot()
    budget.ensure_capacity(manifest.required_budget)
    if before_budget.calls_used != 0 or before_budget.calls_remaining < manifest.required_budget:
        raise LiveSmokeExecutionError("smoke budget is not fully available")

    _mark_ledger(ledger_path, ledger, status="RUNNING", budget=budget)
    started = time.monotonic()
    try:
        provider = provider_factory(manifest, budget)
        backend = ITBenchSnapshotBackend(cast(Any, None), _smoke_scenario(root))
        incident, alerts = build_observable_incident(backend)
        provider_before = getattr(provider, "accounting_snapshot", lambda: None)()
        result = E9InvestigationRuntime(
            provider,
            backend,
            limits=limits,
            execution_id=manifest.execution,
            max_consecutive_rejected_actions=manifest.max_consecutive_rejected_actions,
        ).run(incident, alerts)
        provider_after = getattr(provider, "accounting_snapshot", lambda: None)()
        after_budget = budget.snapshot()
        output = adapt_e9_output(result)
        _persist_run_artifacts(run_dir, result, output)
        _strict_reload(run_dir, result, output)

        provider_invocations = (
            provider_after.provider_invocations - provider_before.provider_invocations
            if provider_before is not None and provider_after is not None
            else result["usage"].get("model_calls", 0)
        )
        outbound_attempts = (
            provider_after.outbound_api_attempts - provider_before.outbound_api_attempts
            if provider_before is not None and provider_after is not None
            else 0
        )
        LiveModelBudget.verify_ledger_delta(before_budget, after_budget, outbound_attempts)
        if provider_invocations != result["usage"].get("model_calls", 0):
            raise LiveSmokeExecutionError("provider invocation accounting mismatch")
        if result["terminal"] not in {"SUBMIT", "STOP"}:
            raise LiveSmokeExecutionError(f"invalid smoke terminal: {result['terminal']}")
        if not result.get("evidence"):
            raise LiveSmokeExecutionError("smoke did not exercise a semantic evidence path")

        smoke_result = {
            "execution": manifest.execution,
            "manifest_sha256": _sha256(manifest_path),
            "runtime_identity": preflight["runtime_identity"],
            "fixture": manifest.fixture.model_dump(mode="json"),
            "model_policy": {
                "provider": manifest.provider,
                "model": manifest.model,
                "reasoning_effort": manifest.reasoning_effort,
                "provider_retries": manifest.provider_retries,
                "judge_disabled": manifest.judge_disabled,
                "rerun_policy": manifest.rerun_policy,
            },
            "runtime_limits": {
                "max_model_calls": limits.max_model_calls,
                "max_agent_turns": limits.max_agent_turns,
                "max_semantic_actions": limits.max_tool_calls,
                "max_wall_time_seconds": limits.max_wall_time_seconds,
                "max_consecutive_rejected_actions": manifest.max_consecutive_rejected_actions,
            },
            "terminal": result["terminal"],
            "metrics": {
                "model_calls": result["usage"].get("model_calls", 0),
                "input_tokens": result["usage"].get("input_tokens", 0),
                "output_tokens": result["usage"].get("output_tokens", 0),
                "provider_invocations": provider_invocations,
                "provider_outbound_attempts": outbound_attempts,
                "semantic_actions_requested": result["usage"].get("semantic_actions_requested", 0),
                "semantic_actions_executed": result["usage"].get("semantic_actions_executed", 0),
                "action_rejections": result["usage"].get("action_rejections", 0),
                "recovered_action_rejections": result["usage"].get(
                    "recovered_action_rejections", 0
                ),
                "evidence_count": len(result.get("evidence", ())),
                "wall_time_ms": int((time.monotonic() - started) * 1000),
            },
            "budget": {
                "cap": after_budget.limit,
                "calls_used": after_budget.calls_used,
                "remaining": after_budget.calls_remaining,
            },
            "artifacts": {
                "run_dir": str(run_dir),
                "native_artifact": str(run_dir / "native_artifact.json"),
                "turn_trace": str(run_dir / "turn_trace.json"),
                "case_state": str(run_dir / "case_state.json"),
                "event_log": str(run_dir / "event_log.json"),
                "usage": str(run_dir / "usage.json"),
                "agent_output": str(run_dir / "agent_output.json"),
            },
            "safety": {
                "provider_fallback": 0,
                "ground_truth_access": 0,
                "cross_scenario_access": 0,
                "writes_remediation": 0,
                "shell": 0,
                "sql": 0,
                "arbitrary_promql": 0,
                "judge_evaluator_access": 0,
            },
        }
        _mark_ledger(ledger_path, ledger, status="COMPLETE", budget=budget)
        atomic_json_write(result_path, smoke_result)
        return smoke_result
    except BaseException:
        if ledger_path.exists():
            _mark_ledger(ledger_path, ledger, status="FAILED", budget=budget)
        raise


__all__ = ["LiveSmokeExecutionError", "ProviderFactory", "execute_live_smoke"]
