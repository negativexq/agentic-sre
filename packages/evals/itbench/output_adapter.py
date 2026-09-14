"""Ground-truth-free adapter from native A1 conclusions to ITBench entities."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from packages.evals.itbench.contracts import (
    ITBenchAgentOutput,
    ITBenchEntity,
    ITBenchEntityPrediction,
)


def entities_from_k8s_records(records: Iterable[dict[str, Any]]) -> tuple[ITBenchEntity, ...]:
    """Extract concrete Kubernetes identities from observable object records."""
    entities: dict[str, ITBenchEntity] = {}
    for item in records:
        raw = item.get("record", item)
        body = raw.get("Body") if isinstance(raw, dict) else None
        candidates: list[Any] = [body, raw]
        for candidate in candidates:
            if isinstance(candidate, str):
                try:
                    candidate = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
            if not isinstance(candidate, dict):
                continue
            obj = candidate.get("object", candidate)
            if not isinstance(obj, dict):
                continue
            metadata = obj.get("metadata")
            if not isinstance(metadata, dict):
                continue
            kind = obj.get("kind")
            name = metadata.get("name")
            if isinstance(kind, str) and isinstance(name, str):
                entity = ITBenchEntity(
                    namespace=(
                        metadata.get("namespace")
                        if isinstance(metadata.get("namespace"), str)
                        else None
                    ),
                    kind=kind,
                    name=name,
                )
                entities[entity.canonical] = entity
    return tuple(entities.values())


def _coerce_entity(value: Any, observed: tuple[ITBenchEntity, ...]) -> ITBenchEntity | None:
    if not isinstance(value, str):
        return None
    parts = value.split("/", 2)
    if len(parts) == 3 and all(parts):
        candidate = ITBenchEntity(namespace=parts[0], kind=parts[1], name=parts[2])
        return (
            candidate if candidate.canonical in {item.canonical for item in observed} else candidate
        )
    matches = [item for item in observed if item.name == value]
    return matches[0] if len(matches) == 1 else None


def adapt_a1_output(
    *,
    scenario_id: str,
    incident_id: str,
    native_output: dict[str, Any],
    observed_entities: tuple[ITBenchEntity, ...] = (),
) -> ITBenchAgentOutput:
    """Export one primary A1 conclusion without consulting evaluator data."""
    hypothesis = native_output.get("hypothesis")
    if not isinstance(hypothesis, dict):
        hypothesis = native_output.get("causal_hypothesis", {})
    if not isinstance(hypothesis, dict):
        hypothesis = {}
    raw_entity = hypothesis.get("entity", hypothesis.get("causal_component"))
    entity = _coerce_entity(raw_entity, observed_entities)
    predictions = (
        ()
        if entity is None
        else (
            ITBenchEntityPrediction(
                entity=entity,
                rank=1,
                condition=str(
                    hypothesis.get(
                        "causal_summary", hypothesis.get("reason", "native A1 conclusion")
                    )
                )[:1000]
                or "native A1 conclusion",
            ),
        )
    )
    return ITBenchAgentOutput(
        incident_id=incident_id,
        scenario_id=scenario_id,
        contributing_factor=predictions,
        reasoning=str(hypothesis.get("causal_summary", ""))[:1000],
        native_terminal=str(native_output.get("termination_reason", "UNKNOWN")),
    )


def write_official_output(path: Path, output: ITBenchAgentOutput) -> None:
    """Write the shape consumed by the official loader, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(output.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            import os

            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


__all__ = ["adapt_a1_output", "entities_from_k8s_records", "write_official_output"]
