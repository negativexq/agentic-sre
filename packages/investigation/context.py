"""Bounded context construction that never forwards raw backend dumps."""

import json
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts import Alert, Evidence, Incident


class AlertSummary(BaseModel):
    """Bounded, untrusted alert facts exposed to the investigator."""

    model_config = ConfigDict(extra="forbid", strict=True)

    alert_name: str = Field(min_length=1, max_length=255)
    service: str = Field(min_length=1, max_length=255)
    namespace: str = Field(min_length=1, max_length=255)
    cluster: str = Field(min_length=1, max_length=255)
    severity: str = Field(min_length=1, max_length=32)
    status: str = Field(min_length=1, max_length=32)
    starts_at: datetime
    ends_at: datetime | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_bounds(self) -> "AlertSummary":
        """Keep model-visible alert metadata bounded and secret-free."""
        for name in ("labels", "annotations"):
            values = getattr(self, name)
            if len(values) > 20 or any(
                len(key) > 100 or len(value) > 500 for key, value in values.items()
            ):
                raise ValueError(f"{name} exceeds model context bounds")
        return self

    @classmethod
    def from_alert(cls, alert: Alert) -> "AlertSummary":
        """Project a normalized alert into safe model-facing facts."""
        return cls(
            alert_name=alert.alert_name,
            service=alert.service,
            namespace=alert.namespace,
            cluster=alert.cluster,
            severity=alert.labels.get("severity", "unknown"),
            status=alert.status.value,
            starts_at=alert.starts_at,
            ends_at=alert.ends_at,
            labels=dict(sorted(alert.labels.items())[:20]),
            annotations=dict(sorted(alert.annotations.items())[:20]),
        )


class CompactContextBuilder:
    """Create compact JSON context from incident facts and normalized evidence."""

    def __init__(self, *, max_evidence_items: int = 12, max_observation_chars: int = 1_000) -> None:
        self._max_evidence_items = max_evidence_items
        self._max_observation_chars = max_observation_chars

    def build(
        self,
        incident: Incident,
        evidence: list[Evidence],
        available_tools: tuple[Any, ...],
        *,
        alerts: tuple[Alert, ...] = (),
        progress: tuple[dict[str, Any], ...] = (),
        current_model_call: int = 1,
        max_model_calls: int = 3,
        tool_calls_used: int = 0,
        tool_calls_remaining: int = 8,
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
            "alerts": [AlertSummary.from_alert(alert).model_dump(mode="json") for alert in alerts],
            "evidence": [
                self._evidence_summary(item) for item in evidence[: self._max_evidence_items]
            ],
            "tool_catalog": [self._tool_descriptor(item) for item in available_tools],
            "progress": list(progress[-12:]),
            "execution": {
                "current_model_call": current_model_call,
                "max_model_calls": max_model_calls,
                "future_model_calls_after_this_decision": max(
                    max_model_calls - current_model_call, 0
                ),
                "is_final_model_turn": current_model_call >= max_model_calls,
                "tool_calls_used": tool_calls_used,
                "tool_calls_remaining": tool_calls_remaining,
            },
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    def _tool_descriptor(self, descriptor: Any) -> dict[str, Any]:
        """Serialize a bounded descriptor without backend implementation details."""
        if isinstance(descriptor, str):
            return {"name": descriptor, "version": "1", "purpose": "Read-only investigation tool"}
        if isinstance(descriptor, dict):
            return cast(dict[str, Any], descriptor)
        if hasattr(descriptor, "model_dump"):
            return cast(dict[str, Any], descriptor.model_dump(mode="json"))
        if hasattr(descriptor, "to_dict"):
            return cast(dict[str, Any], descriptor.to_dict())
        raise TypeError("unsupported tool descriptor")

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
