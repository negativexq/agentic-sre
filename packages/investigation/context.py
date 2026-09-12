"""Bounded context construction that never forwards raw backend dumps."""

import json
from typing import Any

from packages.contracts import Evidence, Incident


class CompactContextBuilder:
    """Create compact JSON context from incident facts and normalized evidence."""

    def __init__(self, *, max_evidence_items: int = 12, max_observation_chars: int = 1_000) -> None:
        self._max_evidence_items = max_evidence_items
        self._max_observation_chars = max_observation_chars

    def build(
        self,
        incident: Incident,
        evidence: list[Evidence],
        available_tools: tuple[str, ...],
        *,
        model_calls_remaining: int,
        tool_calls_remaining: int,
    ) -> str:
        """Return deterministic, size-bounded context for a model turn."""
        payload: dict[str, Any] = {
            "incident": {
                "incident_id": str(incident.incident_id),
                "title": incident.title,
                "severity": incident.severity.value,
                "status": incident.status.value,
                "time_window": {
                    "starts_at": incident.created_at.isoformat(),
                    "ends_at": incident.updated_at.isoformat(),
                },
            },
            "evidence": [
                self._evidence_summary(item) for item in evidence[: self._max_evidence_items]
            ],
            "available_tools": list(available_tools),
            "limits": {
                "model_calls_remaining": model_calls_remaining,
                "tool_calls_remaining": tool_calls_remaining,
            },
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    def _evidence_summary(self, evidence: Evidence) -> dict[str, Any]:
        """Keep provenance and a compact observation summary, not raw results."""
        observation = json.dumps(evidence.observation, sort_keys=True, default=str)
        if len(observation) > self._max_observation_chars:
            observation = f"{observation[: self._max_observation_chars]}…"
        return {
            "evidence_id": str(evidence.evidence_id),
            "source_type": evidence.source_type.value,
            "source_system": evidence.source_system,
            "observation_summary": observation,
            "collected_at": evidence.collected_at.isoformat(),
        }
