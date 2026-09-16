"""Prediction-first, checkpointed plumbing for the future official E10 run.

This module deliberately contains no ground-truth loader or judge import.  It
owns only the provider-free manifest/preflight boundary, observable prediction
execution, and immutable prediction sealing.  Local grading lives in
``e10_local_grading`` and is reachable only after seal verification.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from packages.evals.itbench.dataset import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBenchLiteDataset,
)
from packages.evals.itbench.e9_context import E9_CONTEXT_VERSION
from packages.evals.itbench.e9_identity import collect_e9_identity, validate_e9_identity
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_runtime import (
    ITBENCH_E9_PROMPT_VERSION,
    E9InvestigationRuntime,
    E9Limits,
    e9_prompt_hash,
)
from packages.evals.itbench.external_contracts import ITBENCH_EXTERNAL_PROTOCOL_V5
from packages.evals.itbench.incident import build_observable_incident
from packages.evals.itbench.live_smoke_contract import (
    LIVE_SMOKE_RELEVANT_PATHS,
    ITBenchLiveSmokeRuntimeIdentity,
)
from packages.evals.itbench.output_adapter import adapt_e9_output
from packages.evals.itbench.persistence import atomic_json_write
from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend
from packages.model_policy import validate_agent_config, validate_agent_environment
from packages.provider import (
    LiveModelBudget,
    ModelProvider,
    OpenAIProvider,
    ProviderError,
    live_model_config,
)

E10_EXECUTION: Final[str] = "ITB-E10"
E10_EXPERIMENT: Final[str] = "itbench-lite-sre-external-eval-v10"
E10_TRIAL_COUNT: Final[int] = 1
E10_SCENARIO_COUNT: Final[int] = 35
E10_DATA_ROOT: Final[str] = ".local/itbench-lite"
E10_DEFAULT_PREDICTIONS_ROOT: Final[str] = ".local/itbench-e10-predictions"
E10_DEFAULT_SEAL: Final[str] = "docs/benchmarks/itbench-e10-predictions-seal.json"
E10_SMOKE005_RESULT: Final[str] = "docs/benchmarks/itbench-control-live-smoke-005.json"
E10_SMOKE005_REVIEW: Final[str] = "docs/benchmarks/itbench-control-live-smoke-005.md"
E10_OFFICIAL_RELEVANT_PATHS: Final[tuple[str, ...]] = (
    *LIVE_SMOKE_RELEVANT_PATHS,
    "packages/provider/budget.py",
    "packages/evals/itbench/e10_official.py",
    "packages/evals/itbench/e10_local_grading.py",
    "scripts/itbench_e10_execute.py",
    E10_SMOKE005_RESULT,
    E10_SMOKE005_REVIEW,
)

E10_CONTINUABLE_TERMINALS: Final[frozenset[str]] = frozenset(
    {
        "SUBMIT",
        "STOP",
        "MODEL_STEP_LIMIT",
        "PROTOCOL_STALLED",
        "WALL_TIME_LIMIT",
    }
)
E10_RUNTIME_SAFETY_FIELDS: Final[tuple[str, ...]] = (
    "ground_truth_exposure",
    "cross_scenario_evidence",
    "writes",
    "arbitrary_execution",
)


class E10PreflightError(RuntimeError):
    """A future E10 execution was blocked before provider construction."""


class E10PredictionError(RuntimeError):
    """A prediction run was interrupted or failed closed."""


class E10RuntimeLimits(BaseModel):
    """Named immutable copy of the reviewed official E9 runtime envelope."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    max_model_calls: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_agent_turns: int = Field(gt=0)
    max_wall_time_seconds: int = Field(gt=0)
    max_consecutive_rejected_actions: int = Field(ge=0)

    def to_e9_limits(self) -> E9Limits:
        """Convert the frozen E10 envelope at the runtime boundary."""
        return E9Limits(
            max_model_calls=self.max_model_calls,
            max_tool_calls=self.max_tool_calls,
            max_agent_turns=self.max_agent_turns,
            max_wall_time_seconds=self.max_wall_time_seconds,
        )


# These values are intentionally explicit: they are the reviewed historical
# official envelope, not an implicit constructor default or a score-tuning
# choice.
E10_OFFICIAL_RUNTIME_LIMITS: Final[E10RuntimeLimits] = E10RuntimeLimits(
    max_model_calls=12,
    max_tool_calls=24,
    max_agent_turns=12,
    max_wall_time_seconds=240,
    max_consecutive_rejected_actions=2,
)


class E10OfficialManifestV1(BaseModel):
    """Typed, frozen contract for one official 35-by-1 prediction pass."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    execution: str = Field(pattern=r"^ITB-E10$", max_length=32)
    experiment: str = Field(pattern=r"^itbench-lite-sre-external-eval-v10$", max_length=128)
    dataset_revision: str = Field(min_length=1, max_length=255)
    scenario_order: list[str] = Field(min_length=E10_SCENARIO_COUNT, max_length=E10_SCENARIO_COUNT)
    scenario_order_hash: str = Field(min_length=1)
    scenario_count: int = Field(gt=0)
    trial_count: int = Field(gt=0)
    runtime_identity: ITBenchLiveSmokeRuntimeIdentity
    control_policy_hash: str = Field(min_length=1)
    semantic_registry_hash: str = Field(min_length=1)
    semantic_capability_policy_hash: str = Field(min_length=1)
    provider_schema_hash: str = Field(min_length=1)
    context_planner_hash: str = Field(min_length=1)
    candidate_discovery_hash: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1, max_length=128)
    prompt_hash: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1, max_length=128)
    protocol_hash: str = Field(min_length=1)
    context_version: str = Field(min_length=1, max_length=128)
    provider: str = Field(pattern=r"^openai$")
    model: str = Field(pattern=r"^gpt-5\.6-luna$")
    reasoning_effort: str = Field(pattern=r"^none$")
    provider_retries: int = Field(ge=0, le=0)
    runtime_limits: E10RuntimeLimits
    gt_policy: str = Field(pattern=r"^POST_SEAL_ONLY$")
    judge_policy: str = Field(pattern=r"^DEFERRED_UNTIL_PREDICTIONS_FROZEN_AND_HUMAN_AUTHORIZED$")
    resume_policy: str = Field(pattern=r"^CHECKPOINT_ONLY$")
    failure_policy: str = Field(pattern=r"^ABORT_ON_INFRASTRUCTURE_FAILURE$")
    smoke005_result_sha256: str = Field(min_length=1)
    smoke005_review_sha256: str = Field(min_length=1)
    smoke005_v5_contract_gate: str = Field(pattern=r"^PASS$")

    @property
    def ledger_cap(self) -> int:
        """Return the derived worst-case agent ledger capacity."""
        return self.scenario_count * self.runtime_limits.max_model_calls


@dataclass(frozen=True, slots=True)
class E10Checkpoint:
    """One validated immutable scenario checkpoint."""

    scenario_id: str
    trial: int
    terminal: str
    trial_manifest_sha256: str
    native_artifact_sha256: str
    agent_output_sha256: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scenario_order_hash(order: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps(list(order), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _source_hashes(identity: dict[str, Any]) -> dict[str, str]:
    hashes = identity.get("relevant_content_sha256")
    if not isinstance(hashes, dict):
        raise E10PreflightError("runtime identity content hashes are missing")
    return {str(key): str(value) for key, value in hashes.items()}


def _required_hash(identity: dict[str, Any], path: str) -> str:
    value = _source_hashes(identity).get(path)
    if not value:
        raise E10PreflightError(f"required E10 source hash is missing: {path}")
    return value


def build_e10_manifest(
    root: Path,
    relevant_paths: tuple[str, ...] = E10_OFFICIAL_RELEVANT_PATHS,
) -> E10OfficialManifestV1:
    """Build the E10 manifest from current source identity and pinned constants."""
    identity = collect_e9_identity(root, relevant_paths)
    order = list(ITBENCH_SCENARIO_IDS)
    if len(order) != E10_SCENARIO_COUNT:
        raise E10PreflightError("canonical ITBench scenario order is not exactly 35 entries")
    result_path = root / E10_SMOKE005_RESULT
    review_path = root / E10_SMOKE005_REVIEW
    if not result_path.is_file() or not review_path.is_file():
        raise E10PreflightError("immutable SMOKE-005 gate evidence is missing")
    return E10OfficialManifestV1(
        execution=E10_EXECUTION,
        experiment=E10_EXPERIMENT,
        dataset_revision=ITBENCH_DATASET_REVISION,
        scenario_order=order,
        scenario_order_hash=_scenario_order_hash(tuple(order)),
        scenario_count=E10_SCENARIO_COUNT,
        trial_count=E10_TRIAL_COUNT,
        runtime_identity=ITBenchLiveSmokeRuntimeIdentity.model_validate(identity),
        control_policy_hash=_required_hash(identity, "packages/evals/itbench/e9_control.py"),
        semantic_registry_hash=_required_hash(identity, "packages/evals/itbench/e9_semantic.py"),
        semantic_capability_policy_hash=_required_hash(
            identity, "packages/evals/itbench/e9_semantic.py"
        ),
        provider_schema_hash=_required_hash(identity, "packages/provider/openai.py"),
        context_planner_hash=_required_hash(identity, "packages/evals/itbench/e9_context.py"),
        candidate_discovery_hash=_required_hash(identity, "packages/evals/itbench/e9_semantic.py"),
        prompt_version=ITBENCH_E9_PROMPT_VERSION,
        prompt_hash=e9_prompt_hash(),
        protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V5,
        protocol_hash=_required_hash(identity, "packages/evals/itbench/external_contracts.py"),
        context_version=E9_CONTEXT_VERSION,
        provider="openai",
        model="gpt-5.6-luna",
        reasoning_effort="none",
        provider_retries=0,
        runtime_limits=E10_OFFICIAL_RUNTIME_LIMITS,
        gt_policy="POST_SEAL_ONLY",
        judge_policy="DEFERRED_UNTIL_PREDICTIONS_FROZEN_AND_HUMAN_AUTHORIZED",
        resume_policy="CHECKPOINT_ONLY",
        failure_policy="ABORT_ON_INFRASTRUCTURE_FAILURE",
        smoke005_result_sha256=_sha256(result_path),
        smoke005_review_sha256=_sha256(review_path),
        smoke005_v5_contract_gate="PASS",
    )


def load_e10_manifest(path: Path) -> E10OfficialManifestV1:
    """Load one strict E10 manifest without constructing a provider."""
    try:
        return E10OfficialManifestV1.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise E10PreflightError(f"invalid E10 manifest: {path}") from error


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise E10PreflightError(f"invalid JSON artifact: {path}") from error
    if not isinstance(value, dict):
        raise E10PreflightError(f"artifact must be a JSON object: {path}")
    return value


def _validate_smoke005_gate(root: Path, manifest: E10OfficialManifestV1) -> None:
    result_path = root / E10_SMOKE005_RESULT
    review_path = root / E10_SMOKE005_REVIEW
    if _sha256(result_path) != manifest.smoke005_result_sha256:
        raise E10PreflightError("SMOKE-005 result evidence hash mismatch")
    if _sha256(review_path) != manifest.smoke005_review_sha256:
        raise E10PreflightError("SMOKE-005 review evidence hash mismatch")
    result = _read_json(result_path)
    if result.get("classification") != "LIVE_SMOKE_PASS":
        raise E10PreflightError("SMOKE-005 did not pass")
    metrics = result.get("metrics")
    if not isinstance(metrics, dict) or metrics.get("action_rejections") != 0:
        raise E10PreflightError("SMOKE-005 contract rejection metric is not zero")
    if "V5 contract live gate: `PASS`" not in review_path.read_text(encoding="utf-8"):
        raise E10PreflightError("SMOKE-005 V5 contract gate is not PASS")


def validate_e10_preflight(
    root: Path,
    manifest: E10OfficialManifestV1 | dict[str, Any],
    *,
    relevant_paths: tuple[str, ...],
    ledger: dict[str, Any],
    resume: bool = False,
) -> dict[str, Any]:
    """Validate the complete E10 prediction boundary without provider/GT access."""
    typed = (
        manifest
        if isinstance(manifest, E10OfficialManifestV1)
        else E10OfficialManifestV1.model_validate(manifest)
    )
    actual = collect_e9_identity(root, relevant_paths)
    validate_e9_identity(
        actual,
        {"runtime_identity": typed.runtime_identity.model_dump(mode="json")},
    )
    source_fields = {
        "control_policy_hash": "packages/evals/itbench/e9_control.py",
        "semantic_registry_hash": "packages/evals/itbench/e9_semantic.py",
        "semantic_capability_policy_hash": "packages/evals/itbench/e9_semantic.py",
        "provider_schema_hash": "packages/provider/openai.py",
        "context_planner_hash": "packages/evals/itbench/e9_context.py",
        "candidate_discovery_hash": "packages/evals/itbench/e9_semantic.py",
        "protocol_hash": "packages/evals/itbench/external_contracts.py",
    }
    for field, path in source_fields.items():
        if getattr(typed, field) != _required_hash(actual, path):
            raise E10PreflightError(f"E10 manifest content hash mismatch: {field}")
    if typed.dataset_revision != ITBENCH_DATASET_REVISION:
        raise E10PreflightError("E10 dataset revision mismatch")
    if tuple(typed.scenario_order) != ITBENCH_SCENARIO_IDS:
        raise E10PreflightError("E10 scenario order mismatch")
    if typed.scenario_count != E10_SCENARIO_COUNT or typed.trial_count != E10_TRIAL_COUNT:
        raise E10PreflightError("E10 scenario/trial count mismatch")
    if typed.scenario_order_hash != _scenario_order_hash(tuple(typed.scenario_order)):
        raise E10PreflightError("E10 scenario order hash mismatch")
    if typed.provider != "openai" or typed.provider_retries != 0:
        raise E10PreflightError("E10 provider or retry policy mismatch")
    try:
        policy = validate_agent_config(typed.model, typed.reasoning_effort)
    except Exception as error:
        raise E10PreflightError("E10 model policy mismatch") from error
    if typed.runtime_limits != E10_OFFICIAL_RUNTIME_LIMITS:
        raise E10PreflightError("E10 runtime envelope differs from the frozen official limits")
    expected = typed.ledger_cap
    statuses = {"NOT_STARTED", "RUNNING", "COMPLETE"} if resume else {"NOT_STARTED"}
    if (
        ledger.get("execution") != typed.execution
        or ledger.get("purpose") != "AGENT"
        or ledger.get("cap") != expected
        or not isinstance(ledger.get("calls_used"), int)
        or not 0 <= ledger["calls_used"] <= expected
        or ledger.get("remaining") != expected - ledger["calls_used"]
        or ledger.get("status") not in statuses
    ):
        raise E10PreflightError("E10 official ledger is not fresh/resumable or does not match cap")
    dataset = ITBenchLiteDataset.open(root / E10_DATA_ROOT)
    if tuple(item.scenario_id for item in dataset.scenarios()) != ITBENCH_SCENARIO_IDS:
        raise E10PreflightError("E10 dataset scenario order is not canonical")
    _validate_smoke005_gate(root, typed)
    return {
        "provider_constructed": False,
        "ground_truth_access": False,
        "judge_constructed": False,
        "runtime_identity": actual,
        "model_identity": policy.as_dict(),
        "scenario_order_hash": typed.scenario_order_hash,
        "ledger_cap": expected,
        "manifest": typed.model_dump(mode="json"),
    }


def build_e10_ledger(manifest: E10OfficialManifestV1) -> dict[str, Any]:
    """Build a fresh ledger whose cap is derived from the typed manifest."""
    cap = manifest.ledger_cap
    return {
        "execution": manifest.execution,
        "purpose": "AGENT",
        "cap": cap,
        "calls_used": 0,
        "remaining": cap,
        "status": "NOT_STARTED",
    }


def _default_provider_factory(
    manifest: E10OfficialManifestV1, budget: LiveModelBudget
) -> ModelProvider:
    """Construct the real provider only after caller-side preflight succeeds."""
    identity = validate_agent_environment()
    config = live_model_config()
    if not config.enabled or identity.model != manifest.model:
        raise E10PredictionError("E10 live model environment is not explicitly enabled")
    return OpenAIProvider(budget=budget, config=config, max_retry=manifest.provider_retries)


def _provider_snapshot(provider: ModelProvider) -> Any:
    reader = getattr(provider, "accounting_snapshot", None)
    return reader() if callable(reader) else None


def _checkpoint_paths(trial_dir: Path) -> tuple[Path, ...]:
    return tuple(
        trial_dir / name
        for name in (
            "native_artifact.json",
            "agent_output.json",
            "turn_trace.json",
            "case_state.json",
            "event_log.json",
            "usage.json",
            "trial_manifest.json",
        )
    )


def _validate_checkpoint_policy(checkpoint: dict[str, Any], scenario_id: str) -> None:
    """Apply the benchmark terminal and measured-safety allowlists."""
    terminal = checkpoint.get("terminal")
    if terminal == "PROVIDER_ERROR":
        raise E10PredictionError(
            f"provider failure checkpoint requires human review: {scenario_id}"
        )
    if terminal not in E10_CONTINUABLE_TERMINALS:
        raise E10PredictionError(f"UNEXPECTED_RUNTIME_TERMINAL: {scenario_id}: {terminal!r}")
    safety = checkpoint.get("safety")
    if not isinstance(safety, dict):
        raise E10PredictionError(f"runtime safety is missing: {scenario_id}")
    violations = {
        field: safety.get(field) for field in E10_RUNTIME_SAFETY_FIELDS if safety.get(field) != 0
    }
    if violations:
        raise E10PredictionError(f"RUNTIME_SAFETY_VIOLATION: {scenario_id}: {violations}")


def _validate_checkpoint(
    trial_dir: Path, scenario_id: str, *, expected_manifest_sha256: str | None = None
) -> E10Checkpoint:
    if not all(path.is_file() for path in _checkpoint_paths(trial_dir)):
        raise E10PredictionError(f"PARTIAL_SCENARIO_REQUIRES_HUMAN_REVIEW: {scenario_id}")
    checkpoint = _read_json(trial_dir / "trial_manifest.json")
    if checkpoint.get("scenario_id") != scenario_id or checkpoint.get("trial") != 1:
        raise E10PredictionError(f"checkpoint identity mismatch: {scenario_id}")
    _validate_checkpoint_policy(checkpoint, scenario_id)
    if (
        expected_manifest_sha256 is not None
        and checkpoint.get("manifest_sha256") != expected_manifest_sha256
    ):
        raise E10PredictionError(f"checkpoint manifest mismatch: {scenario_id}")
    for field, filename in {
        "native_artifact_sha256": "native_artifact.json",
        "agent_output_sha256": "agent_output.json",
        "turn_trace_sha256": "turn_trace.json",
        "case_state_sha256": "case_state.json",
        "event_log_sha256": "event_log.json",
        "usage_sha256": "usage.json",
    }.items():
        if checkpoint.get(field) != _sha256(trial_dir / filename):
            raise E10PredictionError(f"checkpoint hash mismatch: {scenario_id}/{filename}")
    return E10Checkpoint(
        scenario_id=scenario_id,
        trial=1,
        terminal=str(checkpoint.get("terminal", "UNKNOWN")),
        trial_manifest_sha256=_sha256(trial_dir / "trial_manifest.json"),
        native_artifact_sha256=_sha256(trial_dir / "native_artifact.json"),
        agent_output_sha256=_sha256(trial_dir / "agent_output.json"),
    )


def _persist_prediction(
    trial_dir: Path,
    result: dict[str, Any],
    output: Any,
    checkpoint: dict[str, Any],
) -> E10Checkpoint:
    trial_dir.mkdir(parents=True, exist_ok=True)
    atomic_json_write(trial_dir / "native_artifact.json", result)
    atomic_json_write(trial_dir / "agent_output.json", output.model_dump(mode="json"))
    atomic_json_write(trial_dir / "turn_trace.json", result["turns"])
    atomic_json_write(trial_dir / "case_state.json", result["case_state"])
    atomic_json_write(trial_dir / "event_log.json", result["events"])
    atomic_json_write(trial_dir / "usage.json", result["usage"])
    # Re-load every native artifact and replay before the checkpoint is sealed.
    if _read_json(trial_dir / "native_artifact.json") != result:
        raise E10PredictionError("native prediction artifact reload mismatch")
    if E9CaseMemory.replay(result).projection() != result["case_state"]:
        raise E10PredictionError("prediction event replay mismatch")
    for field, filename in {
        "native_artifact_sha256": "native_artifact.json",
        "agent_output_sha256": "agent_output.json",
        "turn_trace_sha256": "turn_trace.json",
        "case_state_sha256": "case_state.json",
        "event_log_sha256": "event_log.json",
        "usage_sha256": "usage.json",
    }.items():
        checkpoint[field] = _sha256(trial_dir / filename)
    atomic_json_write(trial_dir / "trial_manifest.json", checkpoint)
    return _validate_checkpoint(trial_dir, str(checkpoint["scenario_id"]))


def _mark_ledger(
    path: Path, ledger: dict[str, Any], *, status: str, budget: LiveModelBudget
) -> None:
    snapshot = budget.snapshot()
    atomic_json_write(
        path,
        {
            **ledger,
            "status": status,
            "calls_used": snapshot.calls_used,
            "remaining": snapshot.calls_remaining,
        },
    )


def predict_e10(
    root: Path,
    *,
    manifest_path: Path,
    ledger_path: Path,
    predictions_root: Path,
    seal_path: Path | None = None,
    provider_factory: Callable[[E10OfficialManifestV1, LiveModelBudget], ModelProvider]
    | None = None,
    relevant_paths: tuple[str, ...] = E10_OFFICIAL_RELEVANT_PATHS,
) -> list[E10Checkpoint]:
    """Run or checkpoint the prediction phase; never load GT or invoke a judge."""
    manifest = load_e10_manifest(manifest_path)
    ledger = _read_json(ledger_path)
    existing_entries = tuple(predictions_root.iterdir()) if predictions_root.is_dir() else ()
    if (predictions_root / "prediction_failure.json").is_file():
        raise E10PredictionError("prior E10 prediction failure requires human review")
    preflight = validate_e10_preflight(
        root,
        manifest,
        relevant_paths=relevant_paths,
        ledger=ledger,
        resume=bool(existing_entries),
    )
    if preflight["provider_constructed"] or preflight["ground_truth_access"]:
        raise E10PreflightError("E10 preflight reported an unsafe boundary")
    if (seal_path or root / E10_DEFAULT_SEAL).is_file():
        raise E10PredictionError("prediction seal already exists; E10 predictions are immutable")
    dataset = ITBenchLiteDataset.open(root / E10_DATA_ROOT)
    scenarios = dataset.scenarios()
    predictions_root.mkdir(parents=True, exist_ok=True)
    completed: list[E10Checkpoint] = []
    pending = []
    for scenario in scenarios:
        trial_dir = predictions_root / scenario.scenario_id / "1"
        if trial_dir.exists():
            completed.append(
                _validate_checkpoint(
                    trial_dir,
                    scenario.scenario_id,
                    expected_manifest_sha256=_sha256(manifest_path),
                )
            )
        else:
            pending.append(scenario)
    budget = LiveModelBudget(manifest.ledger_cap, ledger_path=str(ledger_path))
    if pending:
        budget.ensure_capacity(len(pending) * manifest.runtime_limits.max_model_calls)
    if not pending:
        return [
            next(item for item in completed if item.scenario_id == scenario_id)
            for scenario_id in ITBENCH_SCENARIO_IDS
        ]
    budget_before = budget.snapshot()
    _mark_ledger(ledger_path, ledger, status="RUNNING", budget=budget)
    try:
        provider = (provider_factory or _default_provider_factory)(manifest, budget)
    except BaseException as error:
        atomic_json_write(
            predictions_root / "prediction_failure.json",
            {
                "execution": manifest.execution,
                "failure_type": type(error).__name__,
                "error_message_bounded": str(error)[:500],
                "completed_scenarios": [item.scenario_id for item in completed],
            },
        )
        _mark_ledger(ledger_path, ledger, status="FAILED", budget=budget)
        raise E10PredictionError("E10 provider construction failed") from error
    provider_before = _provider_snapshot(provider)
    try:
        for scenario in pending:
            started = time.monotonic()
            before = _provider_snapshot(provider)
            backend = ITBenchSnapshotBackend(dataset, scenario)
            incident, alerts = build_observable_incident(backend)
            result = E9InvestigationRuntime(
                provider,
                backend,
                limits=manifest.runtime_limits.to_e9_limits(),
                execution_id=manifest.execution,
                max_consecutive_rejected_actions=manifest.runtime_limits.max_consecutive_rejected_actions,
            ).run(incident, alerts)
            output = adapt_e9_output(result)
            after = _provider_snapshot(provider)
            provider_delta = (
                after.outbound_api_attempts - before.outbound_api_attempts
                if before is not None and after is not None
                else 0
            )
            provider_invocation_delta = (
                after.provider_invocations - before.provider_invocations
                if before is not None and after is not None
                else result["usage"].get("model_calls", 0)
            )
            if provider_invocation_delta != result["usage"].get("model_calls", 0):
                raise E10PredictionError(
                    f"provider invocation accounting mismatch: {scenario.scenario_id}"
                )
            checkpoint = {
                "execution": manifest.execution,
                "experiment": manifest.experiment,
                "scenario_id": scenario.scenario_id,
                "trial": 1,
                "manifest_sha256": _sha256(manifest_path),
                "runtime_source_sha": manifest.runtime_identity.git_head,
                "runtime_bundle_sha": manifest.runtime_identity.bundle_sha256,
                "dataset_revision": manifest.dataset_revision,
                "scenario_order_hash": manifest.scenario_order_hash,
                "provider": manifest.provider,
                "model": manifest.model,
                "reasoning_effort": manifest.reasoning_effort,
                "provider_retries": manifest.provider_retries,
                "protocol_version": manifest.protocol_version,
                "protocol_hash": manifest.protocol_hash,
                "prompt_version": manifest.prompt_version,
                "prompt_hash": manifest.prompt_hash,
                "provider_schema_hash": manifest.provider_schema_hash,
                "control_policy_hash": manifest.control_policy_hash,
                "semantic_capability_policy_hash": manifest.semantic_capability_policy_hash,
                "runtime_limits": manifest.runtime_limits.model_dump(mode="json"),
                "fixture": {
                    "scenario_id": scenario.scenario_id,
                    "snapshot_path": scenario.snapshot_path,
                    "ground_truth_loaded": False,
                },
                "terminal": result["terminal"],
                "model_calls": result["usage"].get("model_calls", 0),
                "input_tokens": result["usage"].get("input_tokens", 0),
                "output_tokens": result["usage"].get("output_tokens", 0),
                "semantic_actions": result["usage"].get("semantic_actions_executed", 0),
                "action_rejections": result["usage"].get("action_rejections", 0),
                "recovered_action_rejections": result["usage"].get(
                    "recovered_action_rejections", 0
                ),
                "provider_outbound_delta": provider_delta,
                "provider_invocation_delta": provider_invocation_delta,
                "safety": result.get("safety", {}),
                "ground_truth_access": 0,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            completed.append(
                _persist_prediction(
                    predictions_root / scenario.scenario_id / "1", result, output, checkpoint
                )
            )
    except BaseException as error:
        atomic_json_write(
            predictions_root / "prediction_failure.json",
            {
                "execution": manifest.execution,
                "failure_type": type(error).__name__,
                "error_message_bounded": str(error)[:500],
                "completed_scenarios": [item.scenario_id for item in completed],
            },
        )
        _mark_ledger(ledger_path, ledger, status="FAILED", budget=budget)
        raise E10PredictionError("E10 prediction phase aborted") from error
    global_after = _provider_snapshot(provider)
    if global_after is not None and provider_before is not None:
        outbound_delta = global_after.outbound_api_attempts - provider_before.outbound_api_attempts
        budget_after = budget.snapshot()
        try:
            LiveModelBudget.verify_ledger_delta(budget_before, budget_after, outbound_delta)
        except ProviderError as error:
            _mark_ledger(ledger_path, ledger, status="FAILED", budget=budget)
            raise E10PredictionError("E10 provider/ledger accounting mismatch") from error
    _mark_ledger(ledger_path, ledger, status="COMPLETE", budget=budget)
    return [
        next(item for item in completed if item.scenario_id == scenario_id)
        for scenario_id in ITBENCH_SCENARIO_IDS
    ]


def seal_e10_predictions(
    root: Path,
    *,
    manifest_path: Path,
    predictions_root: Path,
    seal_path: Path,
    ledger_path: Path,
) -> dict[str, Any]:
    """Hash and immutably seal all 35 prediction checkpoints."""
    if seal_path.exists():
        raise E10PredictionError("E10 prediction seal already exists")
    manifest = load_e10_manifest(manifest_path)
    identity = collect_e9_identity(root, E10_OFFICIAL_RELEVANT_PATHS)
    validate_e9_identity(
        identity, {"runtime_identity": manifest.runtime_identity.model_dump(mode="json")}
    )
    checkpoints = [
        _validate_checkpoint(
            predictions_root / scenario_id / "1",
            scenario_id,
            expected_manifest_sha256=_sha256(manifest_path),
        )
        for scenario_id in manifest.scenario_order
    ]
    ledger = _read_json(ledger_path)
    if (
        ledger.get("execution") != manifest.execution
        or ledger.get("cap") != manifest.ledger_cap
        or ledger.get("status") != "COMPLETE"
        or not isinstance(ledger.get("calls_used"), int)
        or not isinstance(ledger.get("remaining"), int)
        or ledger["calls_used"] + ledger["remaining"] != manifest.ledger_cap
    ):
        raise E10PredictionError("E10 ledger is not complete/reconciled for sealing")
    payload = {
        "execution": manifest.execution,
        "experiment": manifest.experiment,
        "runtime_source_sha": manifest.runtime_identity.git_head,
        "runtime_bundle_sha": manifest.runtime_identity.bundle_sha256,
        "manifest_sha256": _sha256(manifest_path),
        "dataset_revision": manifest.dataset_revision,
        "scenario_order": list(manifest.scenario_order),
        "scenario_order_hash": manifest.scenario_order_hash,
        "completion_count": len(checkpoints),
        "ledger": ledger,
        "scenarios": [
            {
                "scenario_id": item.scenario_id,
                "trial": item.trial,
                "terminal": item.terminal,
                "agent_output_sha256": item.agent_output_sha256,
                "native_artifact_sha256": item.native_artifact_sha256,
                "trial_manifest_sha256": item.trial_manifest_sha256,
            }
            for item in checkpoints
        ],
    }
    atomic_json_write(seal_path, payload)
    return payload


def verify_e10_seal(
    root: Path, *, manifest_path: Path, predictions_root: Path, seal_path: Path
) -> dict[str, Any]:
    """Recompute every prediction hash before any post-seal GT access."""
    if not seal_path.is_file():
        raise E10PreflightError("E10 prediction seal is missing")
    manifest = load_e10_manifest(manifest_path)
    seal = _read_json(seal_path)
    if seal.get("completion_count") != E10_SCENARIO_COUNT:
        raise E10PreflightError("E10 prediction seal is incomplete")
    if seal.get("scenario_order") != list(manifest.scenario_order):
        raise E10PreflightError("E10 prediction seal scenario order mismatch")
    if seal.get("manifest_sha256") != _sha256(manifest_path):
        raise E10PreflightError("E10 prediction manifest hash changed")
    sealed = {item["scenario_id"]: item for item in seal.get("scenarios", ())}
    if list(sealed) != manifest.scenario_order:
        raise E10PreflightError("E10 prediction seal scenario set/order mismatch")
    for scenario_id in manifest.scenario_order:
        try:
            checkpoint = _validate_checkpoint(
                predictions_root / scenario_id / "1",
                scenario_id,
                expected_manifest_sha256=_sha256(manifest_path),
            )
        except E10PredictionError as error:
            raise E10PreflightError(f"E10 prediction seal invalid: {scenario_id}") from error
        expected = sealed[scenario_id]
        if (
            expected.get("agent_output_sha256") != checkpoint.agent_output_sha256
            or expected.get("native_artifact_sha256") != checkpoint.native_artifact_sha256
            or expected.get("trial_manifest_sha256") != checkpoint.trial_manifest_sha256
        ):
            raise E10PreflightError(f"E10 prediction seal hash mismatch: {scenario_id}")
    return seal


__all__ = [
    "E10OfficialManifestV1",
    "E10_OFFICIAL_RELEVANT_PATHS",
    "E10_OFFICIAL_RUNTIME_LIMITS",
    "E10PreflightError",
    "E10PredictionError",
    "E10RuntimeLimits",
    "build_e10_ledger",
    "build_e10_manifest",
    "load_e10_manifest",
    "predict_e10",
    "seal_e10_predictions",
    "validate_e10_preflight",
    "verify_e10_seal",
]
