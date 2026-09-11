"""Deterministic incident lifecycle."""

from packages.incident.ingestion import IncidentManager, fingerprint_for_alert, normalize_alert
from packages.incident.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    TransitionResult,
    transition,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidTransitionError",
    "IncidentManager",
    "TransitionResult",
    "fingerprint_for_alert",
    "normalize_alert",
    "transition",
]
