"""Offline RCA source rebuilt from one run's persisted evidence alone.

The run's ``EVIDENCE_GATHERED`` boundary names its window end and snapshot
cycle, and its manifest names every row it may know. Nothing else is read: no
cluster, no clock, no provider. Provider-backed reads are not replayed yet and
fail explicitly instead of answering empty.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from packages.rca.live import LiveSource
from packages.rca.manifest import ManifestEntry, alert_from_payload, event_evidence_id
from packages.rca.model import (
    Alert,
    ClusterEvent,
    EntityRef,
    LogRecord,
    ObjectVersion,
    PodStatusObservation,
    ProviderReadFailure,
    ResourcePressure,
    TraceSpanObservation,
    TrafficObservation,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from packages.rca.investigation.environment import InvestigationBackend

# Capabilities answered from persisted base evidence (``LiveSource.supports``).
_BASE_CAPABILITIES = frozenset({"history", "events", "logs", "incident_events", "incident_changes"})


class ProviderReplayNotConfigured(RuntimeError):
    """A provider-backed read was asked of a replay source with no tape playback."""


@dataclass(frozen=True)
class ReplaySource:
    """One run's frozen base evidence: manifest members, snapshot cycle, boundary."""

    run_id: str
    window_end: datetime
    snapshot_cycle_id: int | None
    manifest: tuple[ManifestEntry, ...]
    snapshot_objects: tuple[dict[str, Any], ...]
    _base: LiveSource

    @classmethod
    def from_run(cls, run_id: str, *, session_factory: sessionmaker[Session]) -> ReplaySource:
        """Load the run's boundary, manifest members and exact snapshot cycle."""
        from packages.storage.manifest import load_manifest, load_replay_run

        with session_factory() as session:
            boundary, members = load_replay_run(session, run_id)
            manifest = load_manifest(session, run_id)
        snapshot = members.snapshot
        base = LiveSource(
            incident=str(boundary.incident_id),
            alert_items=[alert_from_payload(item) for item in members.alerts],
            journal=list(members.journal),
            current_objects=list(snapshot.objects) if snapshot else [],
            event_bodies=[body for _, body in members.events],
            event_evidence_ids=[event_evidence_id(version_id) for version_id, _ in members.events],
            error_items=list(members.logs),
            observed_at=boundary.window_end,
            # The live run used its snapshot exactly when it captured one.
            current_is_live=snapshot is not None,
            provider_adapter=None,
            lifecycle_records=members.lifecycle,
            snapshot_cycle_id=snapshot.cycle_id if snapshot else None,
            snapshot_observed_at=snapshot.observed_at if snapshot else None,
        )
        return cls(
            run_id=run_id,
            window_end=boundary.window_end,
            snapshot_cycle_id=boundary.snapshot_cycle_id,
            manifest=manifest,
            snapshot_objects=snapshot.objects if snapshot else (),
            _base=base,
        )

    def incident_id(self) -> str:
        return self._base.incident_id()

    def observation_cutoff(self) -> datetime | None:
        return self.window_end

    def alerts(self) -> Sequence[Alert]:
        return self._base.alerts()

    def object_history(self) -> Mapping[EntityRef, Sequence[ObjectVersion]]:
        return self._base.object_history()

    def events(self) -> Sequence[ClusterEvent]:
        return self._base.events()

    def logs(self, service: str, *, limit: int = 20) -> Sequence[dict[str, Any]]:
        return self._base.logs(service, limit=limit)

    def error_logs(self) -> Sequence[LogRecord]:
        return self._base.error_logs()

    def pod_status_observations(self) -> Sequence[PodStatusObservation]:
        return self._base.pod_status_observations()

    def traffic_observations(self) -> Sequence[TrafficObservation]:
        # The live source answers this without any provider; replay matches it.
        return self._base.traffic_observations()

    def trace_observations(self) -> Sequence[TraceSpanObservation]:
        # The live source answers this without any provider; replay matches it.
        return self._base.trace_observations()

    def resource_pressure(
        self, pods: Sequence[EntityRef], since: datetime
    ) -> Sequence[ResourcePressure] | ProviderReadFailure:
        raise ProviderReplayNotConfigured("resource_pressure needs provider tape replay")

    def supports(self, capability: str) -> bool:
        if capability in _BASE_CAPABILITIES:
            return self._base.supports(capability)
        raise ProviderReplayNotConfigured(f"{capability} support needs provider tape replay")

    def supports_typed_runtime(self, capability: str) -> bool:
        raise ProviderReplayNotConfigured(f"typed {capability} needs provider tape replay")

    def investigation_backend(self) -> InvestigationBackend:
        raise ProviderReplayNotConfigured("investigation reads need provider tape replay")


__all__ = ["ProviderReplayNotConfigured", "ReplaySource"]
