"""Bounded, ground-truth-free E11 model context assembly."""

from __future__ import annotations

import json
from typing import Any

from packages.evals.itbench.e11_observability import RankedCandidate

E11_CONTEXT_VERSION = "itbench_e11_compact_context_v1"
E11_CONTEXT_MAX_CHARS = 20_000


def build_e11_context(
    *,
    incident: dict[str, Any],
    candidates: tuple[RankedCandidate, ...],
    phase: str,
    remaining_model_calls: int,
    remaining_semantic_actions: int,
    topology: tuple[dict[str, Any], ...] = (),
    max_chars: int = E11_CONTEXT_MAX_CHARS,
) -> str:
    """Build a compact deterministic context packet without raw telemetry."""
    if max_chars < 1:
        raise ValueError("context bound must be positive")
    packet: dict[str, Any] = {
        "context_version": E11_CONTEXT_VERSION,
        "phase": phase,
        "remaining_model_calls": remaining_model_calls,
        "remaining_semantic_actions": remaining_semantic_actions,
        "incident": _bounded_mapping(incident),
        "active_candidates": [candidate.as_dict() for candidate in candidates],
        "directed_topology": list(topology[:24]),
    }
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) <= max_chars:
        return encoded
    # Preserve policy and candidate handles first; evidence details are already
    # persisted by the runtime and can be retrieved by controlled operations.
    packet["directed_topology"] = []
    for candidate in packet["active_candidates"]:
        candidate["retrieval_evidence"] = candidate["retrieval_evidence"][:3]
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > max_chars:
        encoded = json.dumps(
            {
                "context_version": E11_CONTEXT_VERSION,
                "phase": phase,
                "remaining_model_calls": remaining_model_calls,
                "remaining_semantic_actions": remaining_semantic_actions,
                "active_handles": [candidate.handle for candidate in candidates],
            },
            separators=(",", ":"),
        )
    if len(encoded) > max_chars:
        raise ValueError("minimal E11 context exceeds the configured bound")
    return encoded


def _bounded_mapping(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[str(key)] = item
        elif isinstance(item, (list, tuple)):
            result[str(key)] = [str(entry)[:240] for entry in item[:12]]
        else:
            result[str(key)] = str(item)[:500]
    return result


__all__ = ["E11_CONTEXT_MAX_CHARS", "E11_CONTEXT_VERSION", "build_e11_context"]
