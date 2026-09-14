"""Crash-safe, per-scenario persistence for frozen A1 live executions."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.evals.a1_graders import A1ScenarioGrade
from packages.evals.live_fixtures import BenchmarkTrial
from packages.investigation.artifacts import A1RunArtifact


class A1CheckpointReference(BaseModel):
    """Immutable reference to one validated scenario checkpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class A1ScenarioCheckpoint(BaseModel):
    """One complete scenario result, durable before the next scenario starts."""

    model_config = ConfigDict(extra="forbid", strict=True)

    checkpoint_version: Literal["a1-scenario-checkpoint-v1"] = "a1-scenario-checkpoint-v1"
    execution_id: str = Field(min_length=1, max_length=100)
    scenario_id: str = Field(min_length=1, max_length=64)
    set: Literal["compatibility", "generalization"]
    fixture: str = Field(min_length=1, max_length=100)
    scenario_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    trial: BenchmarkTrial
    artifact: A1RunArtifact
    grade: A1ScenarioGrade
    configuration_hashes: dict[str, str] = Field(default_factory=dict, max_length=20)
    calls_before: int = Field(ge=0)
    calls_after: int = Field(ge=0)
    calls_consumed: int = Field(ge=0)
    completed_at: str = Field(min_length=1, max_length=64)


class A1PartialRun(BaseModel):
    """Durable record for an invalidated run with completed checkpoints."""

    model_config = ConfigDict(extra="forbid", strict=True)

    artifact_type: Literal["A1_SINGLE_AGENT_LIVE_PARTIAL"]
    execution_id: str = Field(min_length=1, max_length=100)
    status: Literal["INVALIDATED"]
    scenario_order: list[str] = Field(min_length=1, max_length=100)
    completed_scenario_ids: list[str] = Field(max_length=100)
    invalidation_scenario: str = Field(min_length=1, max_length=64)
    invalidation_code: str = Field(min_length=1, max_length=100)
    invalidation_reason: str = Field(min_length=1, max_length=500)
    invalidation_details: dict[str, Any] = Field(default_factory=dict, max_length=30)
    ledger_path: str = Field(min_length=1, max_length=300)
    ledger_before: int = Field(ge=0)
    ledger_after: int = Field(ge=0)
    outbound_attempts: int = Field(ge=0)
    checkpoint_references: list[A1CheckpointReference] = Field(max_length=100)
    phase_ledger_path: str | None = Field(default=None, max_length=300)


def _atomic_write(path: Path, payload: str) -> None:
    """Write validated JSON using a same-directory atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


class A1CheckpointStore:
    """Store and validate checkpoints for exactly one execution revision."""

    def __init__(
        self,
        directory: Path,
        *,
        execution_id: str,
        configuration_hashes: dict[str, str] | None = None,
    ) -> None:
        self.directory = directory
        self.execution_id = execution_id
        self.configuration_hashes = configuration_hashes

    def prepare_new_run(self) -> None:
        """Reject stale checkpoints instead of silently resuming or overwriting."""
        self.directory.mkdir(parents=True, exist_ok=True)
        existing = sorted(
            item.name
            for item in self.directory.iterdir()
            if item.is_file() and item.suffix == ".json"
        )
        if existing:
            raise RuntimeError(
                f"A1 checkpoint directory is not empty for a new execution: {existing}"
            )

    def path_for(self, scenario_id: str) -> Path:
        if not scenario_id or "/" in scenario_id or "\\" in scenario_id:
            raise ValueError("invalid scenario ID for checkpoint path")
        return self.directory / f"{scenario_id}.json"

    def write(self, checkpoint: A1ScenarioCheckpoint) -> A1CheckpointReference:
        """Validate, atomically write, and hash one checkpoint."""
        if checkpoint.execution_id != self.execution_id:
            raise ValueError("checkpoint execution ID does not match store")
        if checkpoint.trial.scenario_id != checkpoint.scenario_id:
            raise ValueError("checkpoint trial scenario ID mismatch")
        if checkpoint.grade.scenario_id != checkpoint.scenario_id:
            raise ValueError("checkpoint grade scenario ID mismatch")
        if (
            self.configuration_hashes is not None
            and checkpoint.configuration_hashes != self.configuration_hashes
        ):
            raise ValueError("checkpoint configuration hashes do not match store")
        path = self.path_for(checkpoint.scenario_id)
        if path.exists():
            raise FileExistsError(f"checkpoint already exists: {path}")
        payload = checkpoint.model_dump_json(indent=2) + "\n"
        # Validate the exact wire payload before it becomes visible as complete.
        A1ScenarioCheckpoint.model_validate_json(payload)
        _atomic_write(path, payload)
        return A1CheckpointReference(
            scenario_id=checkpoint.scenario_id,
            path=str(path),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def load(self, scenario_id: str) -> A1ScenarioCheckpoint:
        """Load one checkpoint through its strict JSON boundary."""
        checkpoint = A1ScenarioCheckpoint.model_validate_json(
            self.path_for(scenario_id).read_text(encoding="utf-8")
        )
        if checkpoint.execution_id != self.execution_id:
            raise ValueError("checkpoint execution ID does not match store")
        if checkpoint.scenario_id != scenario_id:
            raise ValueError("checkpoint scenario ID does not match path")
        if (
            self.configuration_hashes is not None
            and checkpoint.configuration_hashes != self.configuration_hashes
        ):
            raise ValueError("checkpoint configuration hashes do not match store")
        return checkpoint

    def reference(self, scenario_id: str) -> A1CheckpointReference:
        path = self.path_for(scenario_id)
        self.load(scenario_id)
        return A1CheckpointReference(
            scenario_id=scenario_id,
            path=str(path),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def load_ordered(self, scenario_order: list[str]) -> tuple[A1ScenarioCheckpoint, ...]:
        """Load exactly one validated checkpoint for each frozen scenario."""
        if len(set(scenario_order)) != len(scenario_order):
            raise ValueError("frozen scenario order contains duplicates")
        checkpoints = tuple(self.load(scenario_id) for scenario_id in scenario_order)
        if [item.scenario_id for item in checkpoints] != scenario_order:
            raise ValueError("checkpoint order does not match frozen scenario order")
        return checkpoints


def write_partial_run(path: Path, partial: A1PartialRun) -> None:
    """Atomically persist an invalidated-run record."""
    _atomic_write(path, partial.model_dump_json(indent=2) + "\n")


__all__ = [
    "A1CheckpointReference",
    "A1PartialRun",
    "A1ScenarioCheckpoint",
    "A1CheckpointStore",
    "write_partial_run",
]
