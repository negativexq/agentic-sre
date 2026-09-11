"""Deterministic incident lifecycle."""

from packages.incident.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    TransitionResult,
    transition,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidTransitionError",
    "TransitionResult",
    "transition",
]
