"""Crash-safe per-scenario persistence for future ITBench trials."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import ITBenchAgentOutput
from packages.evals.itbench.output_adapter import write_official_output


def atomic_json_write(path: Path, value: Any) -> str:
    """Validate JSON encoding, fsync it, and atomically publish the file."""
    payload = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    json.loads(payload)
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
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ITBenchRunStore:
    """Execution-scoped store that rejects duplicate scenario/trial output."""

    def __init__(self, root: Path, *, execution_id: str) -> None:
        self.root = root
        self.execution_id = execution_id

    def write_trial(
        self, scenario_id: str, trial: int, *, native_artifact: Any, itbench_output: Any, usage: Any
    ) -> str:
        if not scenario_id.startswith("Scenario-") or trial < 1:
            raise ValueError("invalid ITBench scenario/trial identity")
        path = self.root / scenario_id / str(trial) / "itbench_output.json"
        if path.exists():
            raise FileExistsError(f"ITBench trial already exists: {path}")
        payload = {
            "execution_id": self.execution_id,
            "scenario_id": scenario_id,
            "trial": trial,
            "native_artifact": _json_value(native_artifact),
            "itbench_output": _json_value(itbench_output),
            "usage": _json_value(usage),
        }
        digest = atomic_json_write(path, payload)
        atomic_json_write(path.parent / "native_artifact.json", native_artifact)
        atomic_json_write(path.parent / "usage.json", usage)
        if isinstance(itbench_output, ITBenchAgentOutput):
            write_official_output(path.parent / "outputs" / "agent_output.json", itbench_output)
        return digest

    def read_trial(self, scenario_id: str, trial: int) -> dict[str, Any]:
        """Read and validate one persisted trial without consulting ground truth."""
        path = self.root / scenario_id / str(trial) / "itbench_output.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid ITBench trial checkpoint: {path}") from error
        if not isinstance(payload, dict):
            raise ValueError("ITBench trial checkpoint must be an object")
        if payload.get("execution_id") != self.execution_id:
            raise ValueError("ITBench trial execution identity mismatch")
        if payload.get("scenario_id") != scenario_id or payload.get("trial") != trial:
            raise ValueError("ITBench trial scenario identity mismatch")
        for required in ("native_artifact", "itbench_output", "usage"):
            if required not in payload:
                raise ValueError(f"ITBench trial field is missing: {required}")
        return payload

    def write_failure(
        self,
        scenario_id: str,
        trial: int,
        *,
        failure_stage: str,
        error_code: str,
        details: dict[str, Any] | None = None,
        usage: Any = None,
        ledger: Any = None,
    ) -> str:
        """Persist a bounded scenario failure without making it a completed trial."""
        if not scenario_id.startswith("Scenario-") or trial < 1:
            raise ValueError("invalid ITBench scenario/trial identity")
        path = self.root / scenario_id / str(trial) / "failure_artifact.json"
        if path.exists():
            raise FileExistsError(f"ITBench failure already exists: {path}")
        payload = {
            "execution_id": self.execution_id,
            "scenario_id": scenario_id,
            "trial": trial,
            "status": "INVALIDATED",
            "failure_stage": failure_stage[:128],
            "error_code": error_code[:128],
            "details": _bounded_failure_details(details or {}),
            "usage": _json_value(usage),
            "ledger": _json_value(ledger),
        }
        return atomic_json_write(path, payload)


def _json_value(value: Any) -> Any:
    """Convert supported strict models at the persistence boundary."""
    model_dump = getattr(value, "model_dump", None)
    return model_dump(mode="json") if callable(model_dump) else value


def _bounded_failure_details(details: dict[str, Any]) -> dict[str, Any]:
    """Keep invalidation diagnostics useful without retaining provider payloads."""
    encoded = json.dumps(details, sort_keys=True, default=str)
    if len(encoded) <= 4_000:
        return details
    return {"truncated": True, "summary": encoded[:3_900]}


__all__ = ["ITBenchRunStore", "atomic_json_write"]
