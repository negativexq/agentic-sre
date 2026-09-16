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
from dataclasses import dataclass
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
from packages.provider import (
    LiveModelBudget,
    ModelProvider,
    OpenAIProvider,
    ProviderError,
    live_model_config,
)


class LiveSmokeExecutionError(RuntimeError):
    """A smoke execution was blocked or failed without an automatic rerun."""


ProviderFactory = Callable[[ITBenchLiveSmokeManifestV1, LiveModelBudget], ModelProvider]


@dataclass(frozen=True, slots=True)
class _FailureInfo:
    classification: str
    stage: str
    error: BaseException


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
    persisted: list[str],
) -> None:
    values = (
        ("native_artifact.json", result),
        ("turn_trace.json", result["turns"]),
        ("case_state.json", result["case_state"]),
        ("event_log.json", result["events"]),
        ("usage.json", result["usage"]),
        ("agent_output.json", output.model_dump(mode="json")),
    )
    for name, value in values:
        atomic_json_write(run_dir / name, value)
        persisted.append(name)


def _persist_failure_artifact(
    run_dir: Path,
    manifest: ITBenchLiveSmokeManifestV1,
    failure: _FailureInfo,
    persisted: list[str],
) -> None:
    name = "failure_artifact.json"
    if name in persisted or (run_dir / name).exists():
        return
    atomic_json_write(
        run_dir / name,
        {
            "execution": manifest.execution,
            "status": "FAILED",
            "failure_stage": failure.stage,
            "classification": failure.classification,
            "error_type": type(failure.error).__name__,
            "error_code": _error_code(failure.error),
            "error_message_bounded": str(failure.error)[:1_000],
        },
    )
    persisted.append(name)


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


def _accounting_snapshot(provider: ModelProvider | None) -> Any:
    if provider is None:
        return None
    reader = getattr(provider, "accounting_snapshot", None)
    if not callable(reader):
        return None
    try:
        return reader()
    except BaseException:
        return None


def _error_code(error: BaseException) -> str | None:
    code = getattr(error, "code", None)
    if code is None:
        return None
    value = getattr(code, "value", code)
    return str(value)


def _runtime_measured_safety(result: dict[str, Any] | None) -> dict[str, Any]:
    raw = result.get("safety") if isinstance(result, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "ground_truth_exposure": raw.get("ground_truth_exposure", "not_available"),
        "cross_scenario_evidence": raw.get("cross_scenario_evidence", "not_available"),
        "writes": raw.get("writes", "not_available"),
        "arbitrary_execution": raw.get("arbitrary_execution", "not_available"),
    }


def _structural_safety_invariants() -> dict[str, bool]:
    """Return architectural facts, distinct from runtime-measured counters."""
    return {
        "judge_path_present": False,
        "ground_truth_loader_present": False,
        "provider_fallback_available": False,
        "shell_execution_path_present": False,
        "sql_execution_path_present": False,
        "arbitrary_promql_path_present": False,
        "remediation_path_present": False,
        "cross_scenario_loader_present": False,
    }


def _safety_is_zero(result: dict[str, Any]) -> bool:
    measured = _runtime_measured_safety(result)
    return all(measured.get(name) == 0 for name in measured)


def _identity_payload(
    manifest: ITBenchLiveSmokeManifestV1, preflight: dict[str, Any], manifest_path: Path
) -> dict[str, Any]:
    return {
        "manifest_sha256": _sha256(manifest_path),
        "runtime_identity": preflight["runtime_identity"],
        "identity": {
            "runtime_source_sha": preflight["runtime_identity"]["git_head"],
            "runtime_bundle_sha": preflight["runtime_identity"]["bundle_sha256"],
            "dataset_revision": manifest.dataset_revision,
            "control_policy_hash": manifest.control_policy_hash,
            "semantic_registry_hash": manifest.semantic_registry_hash,
            "semantic_capability_policy_hash": manifest.semantic_capability_policy_hash,
            "provider_schema_hash": manifest.provider_schema_hash,
            "context_planner_hash": manifest.context_planner_hash,
            "candidate_discovery_hash": manifest.candidate_discovery_hash,
            "prompt_version": manifest.prompt_version,
            "prompt_hash": manifest.prompt_hash,
            "protocol_version": manifest.protocol_version,
            "protocol_hash": manifest.protocol_hash,
        },
    }


def _summary(
    *,
    root: Path,
    manifest: ITBenchLiveSmokeManifestV1,
    manifest_path: Path,
    preflight: dict[str, Any],
    result: dict[str, Any] | None,
    failure: _FailureInfo | None,
    provider_before: Any,
    provider_after: Any,
    budget_snapshot: Any,
    persisted: list[str],
    run_dir: Path,
    started: float,
) -> dict[str, Any]:
    usage = result.get("usage", {}) if isinstance(result, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    if provider_before is not None and provider_after is not None:
        provider_invocations = (
            provider_after.provider_invocations - provider_before.provider_invocations
        )
        outbound_attempts = (
            provider_after.outbound_api_attempts - provider_before.outbound_api_attempts
        )
    else:
        provider_invocations = usage.get("provider_invocations")
        outbound_attempts = usage.get("outbound_api_attempts")
    if provider_before is not None and provider_after is not None:
        provider_accounting: Any = {
            "before": provider_before.model_dump(mode="json"),
            "after": provider_after.model_dump(mode="json"),
        }
    elif provider_after is not None:
        provider_accounting = provider_after.model_dump(mode="json")
    else:
        provider_accounting = "not_available"
    budget = (
        {
            "cap": budget_snapshot.limit,
            "calls_used": budget_snapshot.calls_used,
            "remaining": budget_snapshot.calls_remaining,
        }
        if budget_snapshot is not None
        else {
            "cap": manifest.required_budget,
            "calls_used": None,
            "remaining": None,
        }
    )
    summary = {
        "execution": manifest.execution,
        **_identity_payload(manifest, preflight, manifest_path),
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
            "max_model_calls": manifest.max_model_calls,
            "max_agent_turns": manifest.max_agent_turns,
            "max_semantic_actions": manifest.max_semantic_actions,
            "max_wall_time_seconds": manifest.max_wall_time_seconds,
            "max_consecutive_rejected_actions": manifest.max_consecutive_rejected_actions,
        },
        "classification": failure.classification if failure else "LIVE_SMOKE_PASS",
        "failure_stage": failure.stage if failure else None,
        "error_type": type(failure.error).__name__ if failure else None,
        "error_code": _error_code(failure.error) if failure else None,
        "error_message_bounded": str(failure.error)[:1_000] if failure else None,
        "terminal": result.get("terminal") if isinstance(result, dict) else None,
        "metrics": {
            "model_calls": usage.get("model_calls"),
            "provider_invocations": provider_invocations,
            "provider_outbound_attempts": outbound_attempts,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "semantic_actions_requested": usage.get("semantic_actions_requested"),
            "semantic_actions_executed": usage.get("semantic_actions_executed"),
            "action_rejections": usage.get("action_rejections"),
            "recovered_action_rejections": usage.get("recovered_action_rejections"),
            "evidence_count": len(result.get("evidence", ())) if isinstance(result, dict) else None,
            "wall_time_ms": int((time.monotonic() - started) * 1000),
        },
        "provider_accounting": provider_accounting,
        "budget": budget,
        "artifacts": {
            "run_dir": str(run_dir),
            "persisted": list(persisted),
        },
        "runtime_measured_safety": _runtime_measured_safety(result),
        "structural_safety_invariants": _structural_safety_invariants(),
    }
    return summary


def _classify_exception(error: BaseException, stage: str) -> _FailureInfo:
    if stage in {"PROVIDER_CONSTRUCTION", "PROVIDER_TRANSPORT"} or (
        isinstance(error, ProviderError) and stage == "RUNTIME"
    ):
        return _FailureInfo("LIVE_SMOKE_PROVIDER_FAILURE", stage, error)
    return _FailureInfo("LIVE_SMOKE_HARNESS_FAILURE", stage, error)


def _terminal_failure(result: dict[str, Any]) -> _FailureInfo | None:
    terminal = result.get("terminal")
    if terminal in {"MODEL_STEP_LIMIT", "PROTOCOL_STALLED", "WALL_TIME_LIMIT"}:
        return _FailureInfo(
            "LIVE_SMOKE_INTERACTION_NOT_READY",
            "TERMINAL_POLICY",
            RuntimeError(f"runtime terminal: {terminal}"),
        )
    if terminal == "PROVIDER_ERROR":
        return _FailureInfo(
            "LIVE_SMOKE_PROVIDER_FAILURE",
            "PROVIDER_TRANSPORT",
            RuntimeError("runtime returned PROVIDER_ERROR"),
        )
    if terminal not in {"SUBMIT", "STOP"}:
        return _FailureInfo(
            "LIVE_SMOKE_HARNESS_FAILURE",
            "TERMINAL_POLICY",
            RuntimeError(f"invalid smoke terminal: {terminal}"),
        )
    if not result.get("evidence"):
        return _FailureInfo(
            "LIVE_SMOKE_HARNESS_FAILURE",
            "TERMINAL_POLICY",
            RuntimeError("smoke did not exercise a semantic evidence path"),
        )
    if not _safety_is_zero(result):
        return _FailureInfo(
            "LIVE_SMOKE_HARNESS_FAILURE",
            "RUNTIME",
            RuntimeError("runtime measured safety counter is nonzero"),
        )
    return None


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
    stage = "PROVIDER_CONSTRUCTION"
    provider: ModelProvider | None = None
    provider_before: Any = None
    provider_after: Any = None
    result: dict[str, Any] | None = None
    persisted: list[str] = []

    def write_failure(failure: _FailureInfo) -> None:
        """Publish one bounded failure summary before propagating the failure."""
        nonlocal provider_after
        if provider_after is None:
            provider_after = _accounting_snapshot(provider)
        try:
            budget_snapshot = budget.snapshot()
        except BaseException:
            budget_snapshot = None
        _persist_failure_artifact(run_dir, manifest, failure, persisted)
        summary = _summary(
            root=root,
            manifest=manifest,
            manifest_path=manifest_path,
            preflight=preflight,
            result=result,
            failure=failure,
            provider_before=provider_before,
            provider_after=provider_after,
            budget_snapshot=budget_snapshot,
            persisted=persisted,
            run_dir=run_dir,
            started=started,
        )
        if result_path.exists():
            raise LiveSmokeExecutionError("live smoke result already exists; result is immutable")
        atomic_json_write(result_path, summary)
        _mark_ledger(ledger_path, ledger, status="FAILED", budget=budget)

    try:
        provider = provider_factory(manifest, budget)
        provider_before = _accounting_snapshot(provider)
        stage = "RUNTIME"
        backend = ITBenchSnapshotBackend(cast(Any, None), _smoke_scenario(root))
        incident, alerts = build_observable_incident(backend)
        result = E9InvestigationRuntime(
            provider,
            backend,
            limits=limits,
            execution_id=manifest.execution,
            max_consecutive_rejected_actions=manifest.max_consecutive_rejected_actions,
        ).run(incident, alerts)
        provider_after = _accounting_snapshot(provider)
        after_budget = budget.snapshot()
        output = adapt_e9_output(result)
        stage = "PERSISTENCE"
        _persist_run_artifacts(run_dir, result, output, persisted)
        stage = "REPLAY"
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
        stage = "ACCOUNTING_RECONCILIATION"
        LiveModelBudget.verify_ledger_delta(before_budget, after_budget, outbound_attempts)
        if provider_invocations != result["usage"].get("model_calls", 0):
            raise LiveSmokeExecutionError("provider invocation accounting mismatch")
        stage = "TERMINAL_POLICY"
        failure = _terminal_failure(result)
        if failure is not None:
            write_failure(failure)
            raise LiveSmokeExecutionError(f"{failure.classification}: {failure.error}")

        summary = _summary(
            root=root,
            manifest=manifest,
            manifest_path=manifest_path,
            preflight=preflight,
            result=result,
            failure=None,
            provider_before=provider_before,
            provider_after=provider_after,
            budget_snapshot=after_budget,
            persisted=persisted,
            run_dir=run_dir,
            started=started,
        )
        if result_path.exists():
            raise LiveSmokeExecutionError("live smoke result already exists; result is immutable")
        atomic_json_write(result_path, summary)
        _mark_ledger(ledger_path, ledger, status="COMPLETE", budget=budget)
        return summary
    except LiveSmokeExecutionError as error:
        # Terminal-policy failures are deliberately raised only after their
        # summary is published.  Do not attempt to overwrite that evidence.
        if result_path.exists():
            raise
        failure = _classify_exception(error, stage)
        write_failure(failure)
        raise
    except BaseException as error:
        failure = _classify_exception(error, stage)
        try:
            write_failure(failure)
        except BaseException as persist_error:
            raise persist_error from error
        raise LiveSmokeExecutionError(f"{failure.classification}: {failure.error}") from error


__all__ = ["LiveSmokeExecutionError", "ProviderFactory", "execute_live_smoke"]
