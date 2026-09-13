"""Bounded context construction that never forwards raw backend dumps."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts import Alert, AlertStatus, Evidence, Incident, TimeWindow
from packages.investigation.topology import TopologyRegistry, target_from_tool_arguments


class InvestigationObservationWindow(BaseModel):
    """Authoritative bounded window derived from normalized production alerts."""

    model_config = ConfigDict(extra="forbid", strict=True)

    starts_at: datetime
    ends_at: datetime
    source: str = Field(min_length=1)
    temporal_mode: str = Field(default="INCIDENT_WINDOW", min_length=1)

    def time_window(self) -> TimeWindow:
        """Return the contract consumed by evidence provenance."""
        return TimeWindow(starts_at=self.starts_at, ends_at=self.ends_at)


def derive_observation_window(
    incident: Incident,
    alerts: tuple[Alert, ...],
    *,
    now: datetime | None = None,
    maximum_seconds: int = 900,
) -> InvestigationObservationWindow:
    """Derive a deterministic union window; the model cannot choose it."""
    if alerts:
        start = min(alert.starts_at for alert in alerts)
        resolved_ends = [alert.ends_at for alert in alerts if alert.ends_at is not None]
        active = any(alert.status is AlertStatus.FIRING for alert in alerts)
        end = (now or datetime.now(UTC)) if active else max(resolved_ends or [incident.updated_at])
        source = "ACTIVE_ALERT" if active else "RESOLVED_ALERT"
    else:
        start, end, source = incident.created_at, incident.updated_at, "INCIDENT_FALLBACK"
    if end < start:
        end = start
    if (end - start).total_seconds() > maximum_seconds:
        start = end - timedelta(seconds=maximum_seconds)
    return InvestigationObservationWindow(
        starts_at=start,
        ends_at=end,
        source=source,
        temporal_mode="INCIDENT_WINDOW",
    )


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
        observation_window: InvestigationObservationWindow | None = None,
        topology: TopologyRegistry | None = None,
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
            "observation_window": (
                observation_window.model_dump(mode="json") if observation_window else None
            ),
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
        if topology is not None:
            payload["topology"] = topology.serialize()
            queried_workloads = {
                item.target_workload for item in evidence if item.target_workload is not None
            }
            queried_resources = {
                item.target_resource for item in evidence if item.target_resource is not None
            }
            # Progress contains canonical arguments for successful, failed and
            # reused requests. This keeps investigation state faithful to
            # validated tool activity rather than only successful evidence.
            for item in progress:
                arguments = item.get("arguments") if isinstance(item, dict) else None
                if not isinstance(arguments, dict):
                    continue
                target = target_from_tool_arguments(arguments)
                if target.workload is not None:
                    queried_workloads.add(target.workload.value)
                if target.resource is not None:
                    queried_resources.add(target.resource.value)
            payload["investigation_state"] = {
                "alert_scope": alerts[0].service if alerts else None,
                "queried_workloads": sorted(queried_workloads),
                "queried_resources": sorted(queried_resources),
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
            "target_workload": evidence.target_workload,
            "target_resource": evidence.target_resource,
            "observation_summary": observation,
            "collected_at": evidence.collected_at.isoformat(),
            "time_window": evidence.time_window.model_dump(mode="json"),
            "temporal_mode": evidence.observation.get("temporal_mode", "INCIDENT_WINDOW"),
        }
