"""Offline snapshot investigation tape: persisted seed reads and backend responses.

No live source is available during replay. Method, arguments and call order must
match, including unsuccessful reads. This is evaluation infrastructure, not a
replacement for the product's persisted provider replay contract.
"""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from packages.rca import model
from packages.rca.investigation.environment import initial_view, investigation_backend
from packages.rca.investigation.tempo import TempoTraceBatch
from packages.rca.source import ObservationSource

_MODELS = {
    name: value
    for name, value in vars(model).items()
    if isinstance(value, type) and issubclass(value, BaseModel)
}
_TEMPO = TypeAdapter(TempoTraceBatch)
_SOURCE_METHODS = frozenset(
    {
        "incident_id",
        "observation_cutoff",
        "alerts",
        "object_history",
        "events",
        "logs",
        "error_logs",
        "resource_pressure",
        "traffic_observations",
        "trace_observations",
        "pod_status_observations",
        "alert_observation_start",
        "alert_episodes",
        "access_ledger",
    }
)


def encode(value: Any) -> Any:
    if isinstance(value, TempoTraceBatch):
        return {"tempo": _TEMPO.dump_python(value, mode="json")}
    if isinstance(value, BaseModel):
        return {"model": type(value).__name__, "value": value.model_dump(mode="json")}
    if isinstance(value, datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, dict):
        return {"mapping": [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple)):
        return {"sequence": [encode(v) for v in value], "tuple": isinstance(value, tuple)}
    return value


def decode(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "tempo" in value:
        return _TEMPO.validate_python(value["tempo"])
    if "model" in value:
        return _MODELS[value["model"]].model_validate(value["value"])
    if "datetime" in value:
        return datetime.fromisoformat(value["datetime"])
    if "mapping" in value:
        return {decode(k): decode(v) for k, v in value["mapping"]}
    items = [decode(v) for v in value["sequence"]]
    return tuple(items) if value["tuple"] else items


class ReadTape:
    def __init__(self, source: ObservationSource | None = None, rows: list[Any] | None = None):
        self.source = initial_view(source) if source is not None else None
        self.backend = investigation_backend(source) if source is not None else None
        self.rows: list[Any] = [] if rows is None else rows
        self.replaying = source is None
        self.cursor = 0

    def call(self, surface: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        request = {
            "surface": surface,
            "method": method,
            "args": encode(args),
            "kwargs": encode(kwargs),
        }
        if self.replaying:
            if self.cursor >= len(self.rows) or self.rows[self.cursor]["request"] != request:
                raise AssertionError(
                    f"recorded source divergence at {self.cursor}: {surface}.{method}"
                )
            row = self.rows[self.cursor]
            self.cursor += 1
            return decode(row["response"])
        target = self.source if surface == "source" else self.backend
        result = getattr(target, method)(*args, **kwargs)
        self.rows.append({"request": request, "response": encode(result)})
        return result

    def save(self, path: Path) -> None:
        blobs: dict[str, Any] = {}
        index = []
        for row in self.rows:
            response = row["response"]
            key = sha256(json.dumps(response, sort_keys=True).encode()).hexdigest()
            blobs.setdefault(key, response)
            index.append({"request": row["request"], "response_id": key})
        path.write_text(
            json.dumps({"format": "causal-source-tape.v1", "rows": index, "responses": blobs})
        )

    @classmethod
    def load(cls, path: Path) -> ReadTape:
        document = json.loads(path.read_text())
        if isinstance(document, list):
            return cls(rows=document)
        return cls(
            rows=[
                {"request": row["request"], "response": document["responses"][row["response_id"]]}
                for row in document["rows"]
            ]
        )

    def assert_consumed(self) -> None:
        assert self.cursor == len(self.rows), "unconsumed source responses"


class RecordedSeed:
    initial_observation_bounded = True

    def __init__(self, tape: ReadTape):
        self.tape = tape

    def investigation_backend(self) -> RecordedBackend:
        return RecordedBackend(self.tape)

    def evidence_coverage_record(self) -> None:
        # the tapes hold no object-channel coverage (m21 §26.6): as a run recorded before it
        return None

    def __getattr__(self, name: str) -> Any:
        if name not in _SOURCE_METHODS:
            raise AttributeError(name)
        return lambda *args, **kwargs: self.tape.call("source", name, args, kwargs)


class RecordedBackend:
    def __init__(self, tape: ReadTape):
        self.tape = tape

    def __getattr__(self, name: str) -> Any:
        if not (name.startswith("query_") or name == "supports"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.tape.call("backend", name, args, kwargs)
