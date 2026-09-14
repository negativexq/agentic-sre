"""Durable infrastructure records for benchmark execution.

These records describe harness progress only.  They are deliberately separate
from the investigator protocol and contain no evaluator truth or model output.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkPhase(StrEnum):
    PREPARE_ENVIRONMENT = "PREPARE_ENVIRONMENT"
    VERIFY_BASELINE = "VERIFY_BASELINE"
    INJECT_FAULT = "INJECT_FAULT"
    VERIFY_FAULT = "VERIFY_FAULT"
    RUN_WORKLOAD = "RUN_WORKLOAD"
    VERIFY_WORKLOAD = "VERIFY_WORKLOAD"
    VERIFY_TRIGGER = "VERIFY_TRIGGER"
    WAIT_FOR_ALERT = "WAIT_FOR_ALERT"
    WAIT_FOR_INCIDENT = "WAIT_FOR_INCIDENT"
    RUN_AGENT = "RUN_AGENT"
    PERSIST_RESULT = "PERSIST_RESULT"
    GRADE = "GRADE"
    CLEANUP_FAULT = "CLEANUP_FAULT"
    VERIFY_RECOVERY = "VERIFY_RECOVERY"
    RECONCILE_BASELINE = "RECONCILE_BASELINE"
    COMPLETE = "COMPLETE"


class PhaseLedgerRecord(BaseModel):
    """One append-only phase transition."""

    model_config = ConfigDict(extra="forbid", strict=True)

    execution_id: str = Field(min_length=1, max_length=100)
    scenario_id: str = Field(min_length=1, max_length=64)
    phase: BenchmarkPhase
    event: str = Field(pattern=r"^(start|end)$")
    wall_timestamp: datetime
    monotonic_timestamp: float = Field(ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    outcome: str | None = Field(default=None, max_length=40)
    error_code: str | None = Field(default=None, max_length=100)
    error_details: dict[str, Any] = Field(default_factory=dict, max_length=20)


class PhaseLedger:
    """Append phase records immediately and tolerate a torn final JSONL line."""

    def __init__(self, path: Path, *, execution_id: str) -> None:
        self.path = path
        self.execution_id = execution_id

    def append(self, record: PhaseLedgerRecord) -> None:
        if record.execution_id != self.execution_id:
            raise ValueError("phase record execution ID does not match ledger")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = record.model_dump_json() + "\n"
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def start(self, scenario_id: str, phase: BenchmarkPhase) -> float:
        monotonic = time.monotonic()
        self.append(
            PhaseLedgerRecord(
                execution_id=self.execution_id,
                scenario_id=scenario_id,
                phase=phase,
                event="start",
                wall_timestamp=datetime.now(UTC),
                monotonic_timestamp=monotonic,
            )
        )
        return monotonic

    def end(
        self,
        scenario_id: str,
        phase: BenchmarkPhase,
        started_monotonic: float,
        *,
        outcome: str = "PASS",
        error_code: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        monotonic = time.monotonic()
        self.append(
            PhaseLedgerRecord(
                execution_id=self.execution_id,
                scenario_id=scenario_id,
                phase=phase,
                event="end",
                wall_timestamp=datetime.now(UTC),
                monotonic_timestamp=monotonic,
                duration_seconds=max(monotonic - started_monotonic, 0.0),
                outcome=outcome,
                error_code=error_code,
                error_details=error_details or {},
            )
        )

    @contextmanager
    def phase(self, scenario_id: str, phase: BenchmarkPhase) -> Iterator[None]:
        started = self.start(scenario_id, phase)
        try:
            yield
        except Exception as error:
            details = getattr(error, "details", {})
            if not isinstance(details, dict):
                details = {"value": str(details)}
            self.end(
                scenario_id,
                phase,
                started,
                outcome="FAIL",
                error_code=str(getattr(error, "code", type(error).__name__)),
                error_details=details,
            )
            raise
        else:
            self.end(scenario_id, phase, started)

    def read(self) -> list[PhaseLedgerRecord]:
        """Read complete JSONL records, ignoring only a torn trailing line."""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[PhaseLedgerRecord] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                records.append(PhaseLedgerRecord.model_validate_json(line))
            except ValueError:
                if index == len(lines) - 1:
                    break
                raise
        return records


class BenchmarkRunStore:
    """Execution-scoped store for phase records and A1 scenario checkpoints."""

    def __init__(
        self,
        directory: Path,
        *,
        execution_id: str,
        configuration_hashes: dict[str, str] | None = None,
    ) -> None:
        self.directory = directory
        self.phase_ledger = PhaseLedger(directory / "phases.jsonl", execution_id=execution_id)
        self._execution_id = execution_id
        self._configuration_hashes = configuration_hashes

    def prepare_new_run(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.phase_ledger.path.exists() and self.phase_ledger.path.stat().st_size:
            raise RuntimeError(f"benchmark run store already exists: {self.directory}")


__all__ = ["BenchmarkPhase", "PhaseLedger", "PhaseLedgerRecord", "BenchmarkRunStore"]
