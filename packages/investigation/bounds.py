"""Deterministic bounds shared by model context and persisted evidence artifacts."""

import json
from typing import Any

MAX_EVIDENCE_SUMMARY_CHARS = 1_000
TRUNCATION_SUFFIX = "\n...[truncated]"


def bound_text(text: str, *, max_chars: int = MAX_EVIDENCE_SUMMARY_CHARS) -> str:
    """Return text no longer than max_chars, preserving a deterministic marker."""
    if max_chars <= len(TRUNCATION_SUFFIX):
        raise ValueError("max_chars must leave room for the truncation suffix")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX


def bounded_observation_summary(
    observation: dict[str, Any], *, max_chars: int = MAX_EVIDENCE_SUMMARY_CHARS
) -> str:
    """Serialize normalized observation data and apply the shared character bound."""
    serialized = json.dumps(observation, sort_keys=True, default=str)
    return bound_text(serialized, max_chars=max_chars)


__all__ = [
    "MAX_EVIDENCE_SUMMARY_CHARS",
    "TRUNCATION_SUFFIX",
    "bound_text",
    "bounded_observation_summary",
]
