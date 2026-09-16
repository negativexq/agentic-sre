"""Frozen E11 manifest, seal, and preflight contracts.

Prediction execution is intentionally dependency-injected: this module does
not import a provider or evaluator.  A future live runner may supply the
approved provider only after preflight; offline qualification uses the fake
canary and the deterministic catalog modules instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field

from packages.evals.itbench.dataset import (
    ITBENCH_DATASET_REVISION,
    ITBENCH_SCENARIO_IDS,
    ITBenchLiteDataset,
)
from packages.evals.itbench.e9_identity import collect_e9_identity, validate_e9_identity
from packages.evals.itbench.e11_context import E11_CONTEXT_VERSION
from packages.evals.itbench.e11_control import E11_PROMPT_VERSION, e11_prompt_hash
from packages.evals.itbench.e11_observability import E11_CATALOG_VERSION, E11_RETRIEVAL_VERSION
from packages.evals.itbench.external_contracts import ITBENCH_EXTERNAL_PROTOCOL_V5
from packages.evals.itbench.live_smoke_contract import (
    LIVE_SMOKE_RELEVANT_PATHS,
    ITBenchLiveSmokeRuntimeIdentity,
)
from packages.evals.itbench.persistence import atomic_json_write

E11_EXECUTION: Final[str] = "ITB-E11"
E11_EXPERIMENT: Final[str] = "itbench-lite-sre-external-eval-v11"
E11_SCENARIO_COUNT: Final[int] = 35
E11_TRIAL_COUNT: Final[int] = 1
E11_OFFICIAL_RELEVANT_PATHS: Final[tuple[str, ...]] = (
    *LIVE_SMOKE_RELEVANT_PATHS,
    "packages/provider/budget.py",
    "packages/evals/itbench/snapshot_backend.py",
    "packages/evals/itbench/e11_context.py",
    "packages/evals/itbench/e11_control.py",
    "packages/evals/itbench/e11_observability.py",
    "packages/evals/itbench/e11_operations.py",
    "packages/evals/itbench/e11_official.py",
    "packages/evals/itbench/e11_canary.py",
    "packages/evals/itbench/e11_runtime.py",
    "packages/evals/itbench/e11_local_grading.py",
    "scripts/itbench_e11_execute.py",
)


class E11ManifestError(RuntimeError):
    """An E11 manifest or prediction boundary is invalid."""


class E11PredictionError(RuntimeError):
    """A checkpointed E11 prediction phase failed closed."""


class E11RuntimeLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    max_model_calls: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_agent_turns: int = Field(gt=0)
    max_wall_time_seconds: int = Field(gt=0)
    max_consecutive_rejected_actions: int = Field(ge=0)


E11_OFFICIAL_RUNTIME_LIMITS: Final[E11RuntimeLimits] = E11RuntimeLimits(
    max_model_calls=12,
    max_tool_calls=24,
    max_agent_turns=12,
    max_wall_time_seconds=240,
    max_consecutive_rejected_actions=2,
)

E11_CHECKPOINT_FILES: Final[tuple[str, ...]] = (
    "native_artifact.json",
    "agent_output.json",
    "turn_trace.json",
    "case_state.json",
    "event_log.json",
    "usage.json",
    "trial_manifest.json",
)
E11_CONTINUABLE_TERMINALS: Final[frozenset[str]] = frozenset(
    {"SUBMIT", "STOP", "MODEL_STEP_LIMIT", "PROTOCOL_STALLED", "WALL_TIME_LIMIT"}
)


class E11OfficialManifestV1(BaseModel):
    """Strict future-run contract; unknown fields are forbidden."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    execution: str = Field(pattern=r"^ITB-E11$")
    experiment: str = Field(pattern=r"^itbench-lite-sre-external-eval-v11$")
    dataset_revision: str = Field(min_length=1)
    scenario_order: list[str] = Field(min_length=35, max_length=35)
    scenario_order_hash: str = Field(min_length=1)
    scenario_count: int = Field(gt=0)
    trial_count: int = Field(gt=0)
    runtime_identity: ITBenchLiveSmokeRuntimeIdentity
    provider: str = Field(pattern=r"^openai$")
    model: str = Field(pattern=r"^gpt-5\.6-luna$")
    reasoning_effort: str = Field(pattern=r"^none$")
    provider_retries: int = Field(ge=0, le=0)
    runtime_limits: E11RuntimeLimits
    gt_policy: str = Field(pattern=r"^POST_SEAL_ONLY$")
    judge_policy: str = Field(pattern=r"^DEFERRED_UNTIL_PREDICTIONS_FROZEN_AND_HUMAN_AUTHORIZED$")
    resume_policy: str = Field(pattern=r"^CHECKPOINT_ONLY$")
    failure_policy: str = Field(pattern=r"^ABORT_ON_INFRASTRUCTURE_FAILURE$")
    prompt_version: str
    prompt_hash: str
    protocol_version: str
    protocol_hash: str
    context_version: str
    catalog_version: str
    retrieval_version: str

    @property
    def ledger_cap(self) -> int:
        return self.scenario_count * self.runtime_limits.max_model_calls


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _order_hash(order: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(list(order), separators=(",", ":")).encode()).hexdigest()


def _required(identity: dict[str, Any], path: str) -> str:
    value = identity.get("relevant_content_sha256", {}).get(path)
    if not isinstance(value, str) or not value:
        raise E11ManifestError(f"missing E11 identity hash: {path}")
    return value


def build_e11_manifest(
    root: Path,
    *,
    execution: str = E11_EXECUTION,
    relevant_paths: tuple[str, ...] = E11_OFFICIAL_RELEVANT_PATHS,
) -> E11OfficialManifestV1:
    """Build a future manifest from the current source identity."""
    identity = collect_e9_identity(root, relevant_paths)
    order = tuple(ITBENCH_SCENARIO_IDS)
    if execution != E11_EXECUTION or len(order) != E11_SCENARIO_COUNT:
        raise E11ManifestError("E11 execution identity or scenario order is invalid")
    return E11OfficialManifestV1(
        execution=execution,
        experiment=E11_EXPERIMENT,
        dataset_revision=ITBENCH_DATASET_REVISION,
        scenario_order=list(order),
        scenario_order_hash=_order_hash(order),
        scenario_count=E11_SCENARIO_COUNT,
        trial_count=E11_TRIAL_COUNT,
        runtime_identity=ITBenchLiveSmokeRuntimeIdentity.model_validate(identity),
        provider="openai",
        model="gpt-5.6-luna",
        reasoning_effort="none",
        provider_retries=0,
        runtime_limits=E11_OFFICIAL_RUNTIME_LIMITS,
        gt_policy="POST_SEAL_ONLY",
        judge_policy="DEFERRED_UNTIL_PREDICTIONS_FROZEN_AND_HUMAN_AUTHORIZED",
        resume_policy="CHECKPOINT_ONLY",
        failure_policy="ABORT_ON_INFRASTRUCTURE_FAILURE",
        prompt_version=E11_PROMPT_VERSION,
        prompt_hash=e11_prompt_hash(),
        protocol_version=ITBENCH_EXTERNAL_PROTOCOL_V5,
        protocol_hash=_required(identity, "packages/evals/itbench/external_contracts.py"),
        context_version=E11_CONTEXT_VERSION,
        catalog_version=E11_CATALOG_VERSION,
        retrieval_version=E11_RETRIEVAL_VERSION,
    )


def load_e11_manifest(path: Path) -> E11OfficialManifestV1:
    """Load one strict E11 manifest without provider or evaluator access."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return E11OfficialManifestV1.model_validate(value)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise E11ManifestError(f"invalid E11 manifest: {path}") from error


def build_e11_ledger(manifest: E11OfficialManifestV1) -> dict[str, Any]:
    """Build a fresh agent ledger with a derived worst-case cap."""
    cap = manifest.ledger_cap
    return {
        "execution": manifest.execution,
        "purpose": "AGENT",
        "cap": cap,
        "calls_used": 0,
        "remaining": cap,
        "status": "NOT_STARTED",
    }


def validate_e11_preflight(
    root: Path,
    manifest: E11OfficialManifestV1 | dict[str, Any],
    *,
    relevant_paths: tuple[str, ...],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """Validate E11 identity/policy/ledger without provider or GT access."""
    typed = (
        manifest
        if isinstance(manifest, E11OfficialManifestV1)
        else E11OfficialManifestV1.model_validate(manifest)
    )
    actual = collect_e9_identity(root, relevant_paths)
    validate_e9_identity(
        actual, {"runtime_identity": typed.runtime_identity.model_dump(mode="json")}
    )
    if (
        typed.dataset_revision != ITBENCH_DATASET_REVISION
        or tuple(typed.scenario_order) != ITBENCH_SCENARIO_IDS
    ):
        raise E11ManifestError("E11 dataset or scenario order mismatch")
    if typed.scenario_order_hash != _order_hash(tuple(typed.scenario_order)):
        raise E11ManifestError("E11 scenario order hash mismatch")
    if typed.scenario_count != E11_SCENARIO_COUNT or typed.trial_count != E11_TRIAL_COUNT:
        raise E11ManifestError("E11 count policy mismatch")
    if (
        typed.provider != "openai"
        or typed.model != "gpt-5.6-luna"
        or typed.reasoning_effort != "none"
        or typed.provider_retries != 0
    ):
        raise E11ManifestError("E11 model policy mismatch")
    if typed.runtime_limits != E11_OFFICIAL_RUNTIME_LIMITS:
        raise E11ManifestError("E11 runtime envelope changed")
    if ledger != build_e11_ledger(typed):
        raise E11ManifestError("E11 ledger is not fresh or does not match derived cap")
    return {
        "provider_constructed": False,
        "ground_truth_access": False,
        "judge_constructed": False,
        "ledger_cap": typed.ledger_cap,
    }


def predict_e11(
    manifest: E11OfficialManifestV1,
    predictions_root: Path,
    executor: Any,
    *,
    manifest_sha256: str = "",
) -> list[dict[str, Any]]:
    """Run a dependency-injected, checkpointed one-trial-per-scenario pass.

    ``executor`` is the only runtime boundary.  Production wiring must inject
    the approved provider after preflight; offline tests inject a deterministic
    fake.  This module itself has no provider or ground-truth dependency.
    """
    failure_path = predictions_root / "prediction_failure.json"
    if failure_path.exists():
        raise E11PredictionError("E11 prediction failure artifact already exists")
    checkpoints: list[dict[str, Any]] = []
    for scenario_id in manifest.scenario_order:
        trial_dir = predictions_root / scenario_id / "1"
        present = [trial_dir / name for name in E11_CHECKPOINT_FILES]
        if trial_dir.exists():
            if not all(path.is_file() for path in present):
                raise E11PredictionError(f"partial scenario requires review: {scenario_id}")
            checkpoints.append(_checkpoint_from_dir(trial_dir, scenario_id))
            continue
        try:
            result = executor(scenario_id)
        except Exception as error:
            _persist_prediction_failure(
                failure_path,
                scenario_id=scenario_id,
                error_code="EXECUTOR_EXCEPTION",
                error_message=str(error),
            )
            raise E11PredictionError(f"E11 executor failed: {scenario_id}") from error
        if not isinstance(result, dict):
            _persist_prediction_failure(
                failure_path,
                scenario_id=scenario_id,
                error_code="EXECUTOR_RESULT_INVALID",
                error_message="executor did not return an artifact mapping",
            )
            raise E11PredictionError(f"executor did not return an artifact mapping: {scenario_id}")
        terminal = result.get("terminal")
        safety = result.get("safety", {})
        _persist_available_artifacts(trial_dir, result)
        if terminal not in E11_CONTINUABLE_TERMINALS:
            _persist_prediction_failure(
                failure_path,
                scenario_id=scenario_id,
                error_code="UNEXPECTED_RUNTIME_TERMINAL",
                error_message=str(terminal),
            )
            raise E11PredictionError(
                f"E11 infrastructure/terminal failure: {scenario_id}: {terminal}"
            )
        if not isinstance(safety, dict) or any(
            safety.get(field) != 0
            for field in (
                "ground_truth_exposure",
                "cross_scenario_evidence",
                "writes",
                "arbitrary_execution",
            )
        ):
            _persist_prediction_failure(
                failure_path,
                scenario_id=scenario_id,
                error_code="RUNTIME_SAFETY_VIOLATION",
                error_message="measured runtime safety counter was nonzero",
            )
            raise E11PredictionError(f"E11 runtime safety violation: {scenario_id}")
        trial_dir.mkdir(parents=True, exist_ok=True)
        for name in E11_CHECKPOINT_FILES[:-1]:
            value = result.get(name.removesuffix(".json"), {})
            atomic_json_write(trial_dir / name, value)
        trial_manifest = {
            "execution": manifest.execution,
            "scenario_id": scenario_id,
            "trial": 1,
            "terminal": terminal,
            "manifest_sha256": manifest_sha256,
            "runtime_identity": manifest.runtime_identity.model_dump(mode="json"),
            "safety": safety,
            "ground_truth_access": 0,
            "judge_access": 0,
            "hashes": {
                name.removesuffix(".json"): _sha256(trial_dir / name)
                for name in E11_CHECKPOINT_FILES[:-1]
            },
        }
        atomic_json_write(trial_dir / "trial_manifest.json", trial_manifest)
        checkpoints.append(_checkpoint_from_dir(trial_dir, scenario_id))
    return checkpoints


def predict_e11_runtime(
    manifest: E11OfficialManifestV1,
    dataset: ITBenchLiteDataset,
    predictions_root: Path,
    provider: Any,
    *,
    manifest_sha256: str = "",
) -> list[dict[str, Any]]:
    """Run the repository-owned E11 loop for each scenario.

    The provider is dependency-injected so offline tests use ``FakeModelProvider``;
    this function itself never constructs OpenAI or reads ground truth.
    """
    from packages.evals.itbench.e11_runtime import E11InvestigationRuntime, E11RuntimeLimits
    from packages.evals.itbench.snapshot_backend import ITBenchSnapshotBackend

    if manifest.scenario_count != len(manifest.scenario_order):
        raise E11ManifestError("manifest scenario count/order mismatch")

    def execute(scenario_id: str) -> dict[str, Any]:
        scenario = dataset._load_scenario(scenario_id)
        backend = ITBenchSnapshotBackend(dataset, scenario, max_rows=50, max_bytes=100_000)
        runtime = E11InvestigationRuntime(
            provider,
            backend,
            limits=E11RuntimeLimits(
                max_model_calls=manifest.runtime_limits.max_model_calls,
                max_tool_calls=manifest.runtime_limits.max_tool_calls,
                max_agent_turns=manifest.runtime_limits.max_agent_turns,
                max_wall_time_seconds=manifest.runtime_limits.max_wall_time_seconds,
                max_consecutive_rejected_actions=manifest.runtime_limits.max_consecutive_rejected_actions,
            ),
            execution_id=manifest.execution,
        )
        return runtime.run()

    return predict_e11(
        manifest,
        predictions_root,
        execute,
        manifest_sha256=manifest_sha256,
    )


def _persist_available_artifacts(trial_dir: Path, result: dict[str, Any]) -> None:
    """Keep trustworthy executor output before classifying a failure."""
    artifact_names = {name.removesuffix(".json") for name in E11_CHECKPOINT_FILES[:-1]}
    values = {name: result[name] for name in artifact_names if name in result}
    if not values:
        return
    trial_dir.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        path = trial_dir / f"{name}.json"
        if not path.exists():
            atomic_json_write(path, value)


def _persist_prediction_failure(
    path: Path, *, scenario_id: str, error_code: str, error_message: str
) -> None:
    """Write one immutable, bounded controlled-failure marker."""
    if path.exists():
        return
    atomic_json_write(
        path,
        {
            "execution": E11_EXECUTION,
            "scenario_id": scenario_id,
            "error_code": error_code,
            "error_message": _bounded_error_message(error_message),
        },
    )


def _bounded_error_message(value: str) -> str:
    """Keep controlled failure markers free of common credential patterns."""
    sanitized = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    sanitized = re.sub(
        r"(?i)Authorization\s*:\s*Bearer\s+\S+", "Authorization: [REDACTED]", sanitized
    )
    return sanitized[:500]


def _checkpoint_from_dir(trial_dir: Path, scenario_id: str) -> dict[str, Any]:
    trial = json.loads((trial_dir / "trial_manifest.json").read_text(encoding="utf-8"))
    if trial.get("scenario_id") != scenario_id or trial.get("trial") != 1:
        raise E11PredictionError(f"checkpoint identity mismatch: {scenario_id}")
    if trial.get("terminal") not in E11_CONTINUABLE_TERMINALS:
        raise E11PredictionError(f"unexpected checkpoint terminal: {scenario_id}")
    safety = trial.get("safety")
    if not isinstance(safety, dict) or any(
        safety.get(field) != 0
        for field in (
            "ground_truth_exposure",
            "cross_scenario_evidence",
            "writes",
            "arbitrary_execution",
        )
    ):
        raise E11PredictionError(f"checkpoint safety violation: {scenario_id}")
    if trial.get("ground_truth_access") != 0 or trial.get("judge_access") != 0:
        raise E11PredictionError(f"checkpoint isolation violation: {scenario_id}")
    hashes = trial.get("hashes", trial)
    for name in E11_CHECKPOINT_FILES[:-1]:
        stem = name.removesuffix(".json")
        expected = hashes.get(stem)
        if expected is None:
            expected = trial.get(f"{stem}_sha256")
        if expected != _sha256(trial_dir / name):
            raise E11PredictionError(f"checkpoint hash mismatch: {scenario_id}/{name}")
    return {
        "scenario_id": scenario_id,
        "terminal": trial.get("terminal"),
        "trial_manifest_sha256": _sha256(trial_dir / "trial_manifest.json"),
        "native_artifact_sha256": _sha256(trial_dir / "native_artifact.json"),
        "agent_output_sha256": _sha256(trial_dir / "agent_output.json"),
    }


def seal_e11_predictions(
    manifest: E11OfficialManifestV1,
    predictions_root: Path,
    seal_path: Path,
    *,
    manifest_sha256: str = "",
) -> dict[str, Any]:
    """Seal exactly one complete, hash-consistent trial per scenario."""
    if seal_path.exists():
        raise E11ManifestError(f"E11 prediction seal already exists: {seal_path}")
    checkpoints: list[dict[str, Any]] = []
    for scenario_id in manifest.scenario_order:
        trial_dir = predictions_root / scenario_id / "1"
        if not all((trial_dir / name).is_file() for name in E11_CHECKPOINT_FILES):
            raise E11ManifestError(f"incomplete E11 checkpoint: {scenario_id}")
        trial = json.loads((trial_dir / "trial_manifest.json").read_text(encoding="utf-8"))
        if trial.get("scenario_id") != scenario_id or trial.get("trial") != 1:
            raise E11ManifestError(f"checkpoint identity mismatch: {scenario_id}")
        if trial.get("terminal") not in E11_CONTINUABLE_TERMINALS:
            raise E11ManifestError(f"unexpected checkpoint terminal: {scenario_id}")
        safety = trial.get("safety")
        if not isinstance(safety, dict) or any(
            safety.get(field) != 0
            for field in (
                "ground_truth_exposure",
                "cross_scenario_evidence",
                "writes",
                "arbitrary_execution",
            )
        ):
            raise E11ManifestError(f"checkpoint safety violation: {scenario_id}")
        checkpoint = {
            "scenario_id": scenario_id,
            "trial": 1,
            "terminal": trial.get("terminal"),
            "agent_output_sha256": _sha256(trial_dir / "agent_output.json"),
            "native_artifact_sha256": _sha256(trial_dir / "native_artifact.json"),
            "trial_manifest_sha256": _sha256(trial_dir / "trial_manifest.json"),
        }
        trial_hashes = trial.get("hashes", trial)
        for key in ("agent_output_sha256", "native_artifact_sha256"):
            expected_key = key.removesuffix("_sha256")
            if trial_hashes.get(expected_key, trial.get(key)) != checkpoint[key]:
                raise E11ManifestError(f"checkpoint hash mismatch: {scenario_id}: {key}")
        checkpoints.append(checkpoint)
    payload = {
        "execution": manifest.execution,
        "manifest_sha256": manifest_sha256,
        "runtime_identity": manifest.runtime_identity.model_dump(mode="json"),
        "dataset_revision": manifest.dataset_revision,
        "scenario_order": manifest.scenario_order,
        "completion_count": len(checkpoints),
        "checkpoints": checkpoints,
        "ground_truth_access": 0,
        "judge_access": 0,
    }
    atomic_json_write(seal_path, payload)
    return cast(dict[str, Any], payload)


def verify_e11_seal(
    manifest: E11OfficialManifestV1, predictions_root: Path, seal_path: Path
) -> dict[str, Any]:
    """Verify every sealed hash before any post-seal evaluator access."""
    try:
        payload = json.loads(seal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise E11ManifestError("invalid E11 prediction seal") from error
    if (
        payload.get("execution") != manifest.execution
        or payload.get("completion_count") != E11_SCENARIO_COUNT
        or tuple(item.get("scenario_id") for item in payload.get("checkpoints", ()))
        != tuple(manifest.scenario_order)
    ):
        raise E11ManifestError("E11 prediction seal is incomplete")
    for checkpoint in payload.get("checkpoints", ()):
        scenario_id = checkpoint["scenario_id"]
        trial_dir = predictions_root / scenario_id / "1"
        current = _checkpoint_from_dir(trial_dir, scenario_id)
        if (
            current["agent_output_sha256"] != checkpoint.get("agent_output_sha256")
            or current["native_artifact_sha256"] != checkpoint.get("native_artifact_sha256")
            or current["trial_manifest_sha256"] != checkpoint.get("trial_manifest_sha256")
        ):
            raise E11ManifestError(f"E11 prediction seal hash mismatch: {scenario_id}")
    return cast(dict[str, Any], payload)


__all__ = [
    "E11ManifestError",
    "E11PredictionError",
    "E11OfficialManifestV1",
    "E11RuntimeLimits",
    "E11_OFFICIAL_RELEVANT_PATHS",
    "E11_OFFICIAL_RUNTIME_LIMITS",
    "build_e11_ledger",
    "build_e11_manifest",
    "load_e11_manifest",
    "predict_e11",
    "predict_e11_runtime",
    "seal_e11_predictions",
    "validate_e11_preflight",
    "verify_e11_seal",
]
