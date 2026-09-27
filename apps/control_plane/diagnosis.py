"""Runs the diagnosis engine for stored incidents against the live cluster."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as package_version
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from apps.control_plane.scheduler import (
    ReevaluationConfig,
    SchedulerPass,
    reevaluation_from_environment,
    run_scheduler_pass,
)
from packages.contracts import Alert as ContractAlert
from packages.contracts import Incident, IncidentEvent, IncidentEventType
from packages.rca.engine import EngineConfig, Investigator, diagnose
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.investigation.graph import investigate_diagnosis
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.policy import LLMInvestigationPolicy
from packages.rca.investigation.selection import DeterministicObservationPolicy
from packages.rca.investigation.state import (
    SEED_FULL_SOURCE,
    InvestigationConfig,
    InvestigationPolicy,
    rca_config_digest,
)
from packages.rca.lifecycle import classify, status_payload
from packages.rca.live import (
    ChangeWatcher,
    ClusterReader,
    KubernetesClusterReader,
    ListingFailure,
    LiveSource,
    ObjectSnapshot,
    capture_error_logs,
    incident_window,
)
from packages.rca.llm import LLMClient
from packages.rca.manifest import alert_from_payload, event_evidence_id
from packages.rca.model import Alert, Diagnosis
from packages.rca.provider_adapter import ProviderAdapter, ProviderReaders
from packages.storage import (
    AlertRepository,
    DiagnosisRepository,
    EventRepository,
    EvidenceRequirementRepository,
    IncidentEventRepository,
    IncidentNotFoundError,
    IncidentRepository,
    InvestigationRunRepository,
    LogObservationRepository,
    ObjectVersionRepository,
)
from packages.storage.manifest import (
    ManifestRequest,
    build_manifest,
    load_manifest_digest,
    load_members,
)
from packages.storage.repositories import (
    EntityInstanceRepository,
    LifecycleRecord,
    LifecycleRepository,
    SnapshotCycleRepository,
)
from packages.storage.retention import RetentionPolicy, apply_retention, policy_from_environment
from packages.storage.tape import load_tape_digest
from packages.storage.trajectory import TRAJECTORY_ARTIFACT_VERSION, trajectory_document

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"RESOLVED", "CLOSED", "FAILED"})
# LEGACY marks rows stored before revisions; a run never produces it.
_REVISION_TRIGGERS = frozenset({"INITIAL", "MANUAL", "EVIDENCE_DEADLINE"})
_DISTRIBUTION = "agentic-sre"


def engine_version() -> str:
    """The deployed package version; missing package metadata fails loudly."""
    return package_version(_DISTRIBUTION)


_LIFECYCLE_SOURCE = "collector"
_TOMBSTONE_SOURCE = "journal-tombstone"


def _rca_alert(item: ContractAlert) -> Alert:
    return Alert(
        name=item.alert_name,
        service=item.service,
        namespace=item.namespace,
        starts_at=item.starts_at,
        labels={**item.labels, "service_name": item.service},
    )


def _checkpoint(records: list[LifecycleRecord]) -> int | None:
    """Index of the latest STATUS_SNAPSHOT: the last state whose transitions all persisted."""
    for index in range(len(records) - 1, -1, -1):
        if records[index].type == "STATUS_SNAPSHOT":
            return index
    return None


def _previous_pod_body(records: list[LifecycleRecord], uid: str) -> dict[str, Any] | None:
    """The instance's checkpointed state, in the classifier's body shape.

    A STATUS_SNAPSHOT is written only after every transition drafted in its
    cycle was persisted, so it is the last state the ledger fully explains; a
    transition that failed to persist is drafted again from it next cycle.
    Eviction is not part of the projection, so it is restored from the
    ledger's own EVICTED fact.
    """
    checkpoint = _checkpoint(records)
    if checkpoint is None:
        return None
    last = records[checkpoint].payload
    status: dict[str, Any] = {
        "conditions": [last["ready"]] if last.get("ready") else [],
        "containerStatuses": last.get("containerStatuses") or [],
    }
    if any(record.type == "EVICTED" for record in records):
        status["reason"] = "Evicted"
    return {
        "kind": "Pod",
        "metadata": {"uid": uid, "deletionTimestamp": last.get("deletionTimestamp")},
        "status": status,
    }


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class _InstanceIndexer:
    """Keeps the materialized exact-instance index current; it is never evidence."""

    index: EntityInstanceRepository
    namespaces: frozenset[str]

    def observe(self, body: dict[str, Any], observed_at: datetime) -> None:
        metadata = body.get("metadata") or {}
        uid, namespace, kind = metadata.get("uid"), metadata.get("namespace"), body.get("kind")
        if not uid or namespace not in self.namespaces or not isinstance(kind, str):
            return
        owners = [item for item in metadata.get("ownerReferences") or [] if isinstance(item, dict)]
        owner = next(
            (item for item in owners if item.get("controller")), owners[0] if owners else {}
        )
        self.index.upsert(
            namespace=namespace,
            kind=kind,
            name=str(metadata.get("name") or ""),
            uid=uid,
            observed_at=observed_at,
            owner_kind=owner.get("kind"),
            owner_name=owner.get("name"),
            owner_uid=owner.get("uid"),
            created_at=_time(metadata.get("creationTimestamp")),
        )

    def reconcile_deletions(self) -> int:
        """Mark every journal-tombstoned instance the index does not show as deleted yet."""
        repaired = 0
        for tombstone in self.index.tombstones_unmarked(set(self.namespaces)):
            self.index.upsert(
                namespace=tombstone.namespace,
                kind=tombstone.kind,
                name=tombstone.name,
                uid=tombstone.uid,
                observed_at=tombstone.observed_at,
                deleted=True,
            )
            repaired += 1
        return repaired


@dataclass
class _PodLifecycleRecorder:
    """Appends each observed Pod's lifecycle facts to the ledger during one cycle.

    A failed write is logged and counted, and the instance's STATUS_SNAPSHOT
    checkpoint is not advanced, so the next cycle drafts the missing facts
    again. Deletions come only from persisted journal tombstones.
    """

    ledger: LifecycleRepository
    journal: ObjectVersionRepository
    namespaces: frozenset[str]
    snapshot_interval: timedelta
    reset: Callable[[], None]
    failures: int = 0
    repairs: int = 0

    def _append(self, **values: Any) -> bool:
        try:
            self.ledger.append(**values)
        except Exception as error:
            self.reset()
            self.failures += 1
            logger.warning(
                "lifecycle observation was not recorded; it will be retried",
                extra={
                    "lifecycle_type": values.get("type"),
                    "instance_uid": values.get("instance_uid"),
                    "error_type": type(error).__name__,
                },
                exc_info=True,
            )
            return False
        return True

    def observe(self, body: dict[str, Any], observed_at: datetime) -> None:
        metadata = body.get("metadata") or {}
        uid = metadata.get("uid")
        namespace = metadata.get("namespace")
        if body.get("kind") != "Pod" or namespace not in self.namespaces or not uid:
            return
        records = self.ledger.list_for(namespace, "Pod", uid)
        checkpoint = _checkpoint(records)
        since = records[checkpoint + 1 :] if checkpoint is not None else records
        complete = True
        for draft in classify(_previous_pod_body(records, uid), body, observed_at):
            if any(
                record.type == draft.type
                and record.source_at == draft.source_at
                and record.payload.get("containers") == draft.payload.get("containers")
                for record in since
            ):
                continue  # drafted and persisted before the checkpoint failed to advance
            complete &= self._append(
                namespace=draft.namespace,
                kind=draft.kind,
                name=draft.name,
                instance_uid=draft.instance_uid,
                type=draft.type,
                observed_at=draft.observed_at,
                source=_LIFECYCLE_SOURCE,
                payload=draft.payload,
                source_at=draft.source_at,
            )
        if not complete:
            return  # keep the checkpoint where the ledger still explains the state
        payload = status_payload(body)
        last = records[checkpoint] if checkpoint is not None else None
        if (
            last is None
            or last.payload != payload
            or observed_at - last.observed_at >= self.snapshot_interval
        ):
            self._append(
                namespace=namespace,
                kind="Pod",
                name=str(metadata.get("name") or ""),
                instance_uid=uid,
                type="STATUS_SNAPSHOT",
                observed_at=observed_at,
                source=_LIFECYCLE_SOURCE,
                payload=payload,
            )

    def reconcile_deletions(self) -> None:
        """Record DELETED for every Pod journal tombstone the ledger does not reflect yet.

        The tombstone is the evidence: its own namespace, name, UID and
        observation time are used, never the time of this repair.
        """
        for tombstone in self.journal.tombstones_missing_deletion(set(self.namespaces)):
            records = self.ledger.list_for(tombstone.namespace, "Pod", tombstone.uid)
            if self._append(
                namespace=tombstone.namespace,
                kind="Pod",
                name=tombstone.name,
                instance_uid=tombstone.uid,
                type="DELETED",
                observed_at=tombstone.observed_at,
                source=_TOMBSTONE_SOURCE,
                payload=records[-1].payload if records else {},
            ):
                self.repairs += 1


@dataclass(frozen=True)
class SnapshotResult:
    """Evidence captured by one diagnosis/watch snapshot cycle."""

    stored_versions: int
    started_at: datetime
    completed_at: datetime
    objects: tuple[dict[str, Any], ...] = ()
    event_bodies: tuple[dict[str, Any], ...] = ()
    failed_scopes: tuple[ListingFailure, ...] = ()
    event_listing_failed: bool = False
    # Lifecycle ledger/index writes that failed this cycle (retried next cycle)
    # and deletions completed from persisted journal tombstones.
    lifecycle_write_failures: int = 0
    lifecycle_repairs: int = 0
    # The persisted snapshot cycle, when this cycle captured evidence for a run.
    cycle_id: int | None = None


@dataclass
class DiagnosisService:
    """Glue between storage, cluster readers, and the engine."""

    session_factory: sessionmaker[Session]
    namespaces: tuple[str, ...]
    evidence_namespaces: tuple[str, ...] = ("chaos-mesh",)
    reader: ClusterReader | None = None
    investigator_factory: Callable[[], Investigator | None] = lambda: None
    bounded_policy_factory: Callable[[], InvestigationPolicy | None] = lambda: None
    # The product always seeds from the full source; only replay-mechanics
    # tests that need a non-trivial trajectory pick the bounded benchmark seed.
    investigation_seed_mode: str = SEED_FULL_SOURCE
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    provider_readers: ProviderReaders = field(default_factory=ProviderReaders)
    # Serializes ChangeWatcher.snapshot() runs: the periodic watch() loop and a
    # run()-triggered snapshot can otherwise race on the same read-then-write
    # (latest version, then insert) in ObjectVersionRepository.
    #
    # This is a process-local lock: it only protects a single DiagnosisService
    # instance. Today's deployment runs exactly one (one uvicorn worker, one
    # control-plane replica -- see infra/kubernetes/control-plane.yaml), so it
    # is sufficient. It stops being sufficient the moment more than one worker
    # or replica writes to the same database; that needs a database-level
    # lock (e.g. a Postgres advisory lock) instead, or a single writer process.
    _snapshot_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_snapshot_result: SnapshotResult | None = field(default=None, init=False, repr=False)
    # A STATUS_SNAPSHOT with unchanged content is written at most this often.
    status_snapshot_interval: timedelta = timedelta(seconds=30)
    # Evidence retention; None (the default) keeps everything.
    retention_policy: RetentionPolicy | None = None
    # Deadline reevaluation (SRE_REEVALUATE); None (the default) never schedules.
    reevaluation: ReevaluationConfig | None = None
    # Cumulative lifecycle/index write failures and tombstone-based repairs.
    lifecycle_write_failures: int = field(default=0, init=False)
    lifecycle_repairs: int = field(default=0, init=False)

    @property
    def last_snapshot_result(self) -> SnapshotResult | None:
        """Return the latest cycle metadata, including incomplete scopes."""
        return self._last_snapshot_result

    def snapshot(self) -> int:
        """Record changed objects, deletions, and new events; how many were stored."""
        return self.snapshot_result().stored_versions

    def snapshot_result(self, run_id: str | None = None) -> SnapshotResult:
        """Capture one coherent object/Event cycle with an explicit boundary.

        With a ``run_id`` (a diagnosis capture) the listing is also persisted
        as one snapshot cycle with every object's full body; watch cycles
        without a run do not write it.
        """
        if self.reader is None:
            now = self.clock()
            result = SnapshotResult(0, now, now)
            self._last_snapshot_result = result
            return result
        journal_namespaces = tuple(dict.fromkeys((*self.namespaces, *self.evidence_namespaces)))
        with self._snapshot_lock, self.session_factory() as session:
            repository = ObjectVersionRepository(session)
            lifecycle = _PodLifecycleRecorder(
                LifecycleRepository(session),
                repository,
                frozenset(self.namespaces),
                self.status_snapshot_interval,
                session.rollback,
            )
            indexer = _InstanceIndexer(
                EntityInstanceRepository(session), frozenset(self.namespaces)
            )
            failures = 0
            index_repairs = 0

            def best_effort(write: Callable[[], None]) -> None:
                # Lifecycle facts and the index are written after the journal: a
                # failure is logged and counted, never fatal to the journal cycle,
                # and the next cycle drafts or reconciles what is missing.
                nonlocal failures
                try:
                    write()
                except Exception:
                    session.rollback()
                    failures += 1
                    logger.warning(
                        "lifecycle/index write failed; it will be retried next cycle",
                        exc_info=True,
                    )

            def record(body: dict[str, Any], observed_at: datetime) -> bool:
                stored = repository.record(body, observed_at)
                best_effort(lambda: lifecycle.observe(body, observed_at))
                best_effort(lambda: indexer.observe(body, observed_at))
                return stored

            def repair_index() -> None:
                nonlocal index_repairs
                index_repairs = indexer.reconcile_deletions()

            watcher = ChangeWatcher(
                self.reader,
                journal_namespaces,
                record,
                lambda: repository.live_keys(set(journal_namespaces)),
                repository.tombstone,
                self.clock,
            )
            object_snapshot: ObjectSnapshot = watcher.snapshot_result()
            # Only persisted journal tombstones (written for a completely listed
            # scope) end an instance; this also repairs deletions a failed write
            # or a restart left out. Listing absence alone never does.
            best_effort(lifecycle.reconcile_deletions)
            best_effort(repair_index)
            cycle_id: int | None = None
            if run_id is not None:
                # Evidence for the run: a failure here fails the capture.
                listing = object_snapshot.listing
                assert object_snapshot.observed_at is not None
                cycle_id = SnapshotCycleRepository(session).record(
                    run_id=run_id,
                    started_at=object_snapshot.started_at,
                    observed_at=object_snapshot.observed_at,
                    completed_at=object_snapshot.completed_at,
                    completed_scopes=[
                        (scope.namespace, scope.kind) for scope in listing.completed_scopes
                    ],
                    failed_scopes=[
                        (failure.scope.namespace, failure.scope.kind, failure.error)
                        for failure in listing.failed_scopes
                    ],
                    objects=listing.objects,
                )
            event_observed_at = self.clock()
            events = EventRepository(session)
            event_listing_failed = False
            try:
                event_bodies = self.reader.list_events(journal_namespaces)
            except Exception:
                # Object journaling has already committed its own facts. A
                # transient Event-list failure is missing evidence, not a
                # reason to roll back or synthesize object lifecycle state.
                logger.warning(
                    "event snapshot failed; continuing without new Events", exc_info=True
                )
                event_bodies = []
                event_listing_failed = True
            stored = object_snapshot.stored_versions + sum(
                events.record(body, event_observed_at) for body in event_bodies
            )
            cycle_failures = failures + lifecycle.failures
            cycle_repairs = lifecycle.repairs + index_repairs
            self.lifecycle_write_failures += cycle_failures
            self.lifecycle_repairs += cycle_repairs
            result = SnapshotResult(
                stored_versions=stored,
                started_at=object_snapshot.started_at,
                completed_at=self.clock(),
                objects=object_snapshot.listing.objects,
                event_bodies=tuple(event_bodies),
                failed_scopes=object_snapshot.listing.failed_scopes,
                event_listing_failed=event_listing_failed,
                lifecycle_write_failures=cycle_failures,
                lifecycle_repairs=cycle_repairs,
                cycle_id=cycle_id,
            )
            self._last_snapshot_result = result
            if result.failed_scopes:
                logger.warning(
                    "cluster snapshot incomplete for scopes: %s",
                    ", ".join(
                        f"{failure.scope.namespace}/{failure.scope.kind} ({failure.error})"
                        for failure in result.failed_scopes
                    ),
                )
            return result

    def apply_retention(self) -> None:
        """One retention pass when a policy is configured; failures are logged."""
        if self.retention_policy is None:
            return
        try:
            with self._snapshot_lock, self.session_factory() as session:
                result = apply_retention(session, self.retention_policy, self.clock())
            if result.events_deleted or result.lifecycle_deleted or result.objects_deleted:
                logger.info("retention removed %s", result)
        except Exception:
            logger.warning("retention pass failed", exc_info=True)

    def reevaluate(self) -> SchedulerPass | None:
        """One deadline-reevaluation pass when enabled; failures are logged."""
        if self.reevaluation is None:
            return None
        try:
            return run_scheduler_pass(
                self.session_factory, self.reevaluation, now=self.clock(), run=self.run
            )
        except Exception:
            logger.warning("deadline reevaluation pass failed", exc_info=True)
            return None

    def watch(self, stop: threading.Event, interval_seconds: float) -> None:
        """Snapshot the cluster until ``stop`` is set; errors are logged and retried."""
        while not stop.is_set():
            try:
                stored = self.snapshot()
                if stored:
                    logger.info("object journal stored %d changed object(s)", stored)
            except Exception:
                logger.warning("cluster snapshot failed", exc_info=True)
            self.apply_retention()
            self.reevaluate()
            stop.wait(interval_seconds)

    def _emit(
        self,
        incident_id: UUID,
        correlation_id: UUID,
        event_type: IncidentEventType,
        payload: dict[str, Any],
    ) -> None:
        """Append one timeline event for the diagnosis pipeline.

        Best effort: a recording error is logged and swallowed, so timeline
        persistence cannot fail the diagnosis path. It is a synchronous commit,
        so it adds the event-persistence overhead but nothing else.
        """
        try:
            with self.session_factory() as session:
                IncidentEventRepository(session).append(
                    IncidentEvent(
                        incident_id=incident_id,
                        event_type=event_type,
                        timestamp=self.clock(),
                        correlation_id=correlation_id,
                        payload=payload,
                    )
                )
        except Exception:  # noqa: BLE001 - observability must not break diagnosis
            logger.warning("failed to record %s timeline event", event_type.value, exc_info=True)

    def run(self, incident_id: UUID, trigger: str) -> Diagnosis:
        """Diagnose the incident now and store the result as its next revision.

        ``trigger`` is why this revision is produced: ``MANUAL`` for a direct
        request, ``INITIAL`` from auto-diagnosis, ``EVIDENCE_DEADLINE`` from
        the scheduler.
        """
        if trigger not in _REVISION_TRIGGERS:
            raise ValueError(f"not a revision trigger: {trigger!r}")
        run_id = str(uuid4())
        with self.session_factory() as session:
            incident = IncidentRepository(session).get(incident_id)
            if incident is None:
                raise IncidentNotFoundError(str(incident_id))
            correlation_id = incident.correlation_id
            alerts = [
                _rca_alert(item) for item in AlertRepository(session).list_for_incident(incident_id)
            ]
        self._emit(
            incident_id,
            correlation_id,
            IncidentEventType.DIAGNOSIS_STARTED,
            {"run_id": run_id, "alerts": [alert.name for alert in alerts]},
        )
        try:
            return self._diagnose(run_id, incident, incident_id, correlation_id, alerts, trigger)
        except Exception as error:
            # Any failure after the run started — gathering evidence, the engine,
            # or persisting the diagnosis — terminates this run's timeline, so the
            # UI never pairs an incomplete run with an older stored diagnosis.
            self._emit(
                incident_id,
                correlation_id,
                IncidentEventType.DIAGNOSIS_FAILED,
                {"run_id": run_id, "error": type(error).__name__},
            )
            raise

    def _capture_logs(
        self,
        incident_id: UUID,
        alerts: list[Alert],
        listed: tuple[dict[str, Any], ...],
        provider_adapter: ProviderAdapter,
    ) -> None:
        """Bulk Loki capture for an open incident, persisted before the manifest is taken."""
        services = sorted(
            {
                str(body.get("metadata", {}).get("name"))
                for body in listed
                if body.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"}
            }
            | {alert.service for alert in alerts if alert.service}
        )
        # The incident history is wider than one bounded Loki read, so it is
        # captured as several <=1h reads rather than one rejected read.
        starts_at, ends_at = incident_window(alerts, self.clock())
        capture = capture_error_logs(provider_adapter, services, starts_at, ends_at)
        for failure in capture.failed:
            logger.warning(
                "log capture slice [%s, %s) failed (%s: %s); continuing with the rest",
                failure.starts_at.isoformat(),
                failure.ends_at.isoformat(),
                failure.error_type,
                failure.error,
            )
        if capture.skipped:
            logger.info(
                "log capture read the newest %d of %d bounded slices; older slices "
                "rely on earlier persisted captures",
                len(capture.queried),
                len(capture.queried) + len(capture.skipped),
            )
        if not capture.succeeded:
            if capture.queried:
                logger.warning("every log capture slice failed; diagnosing without new logs")
            return
        with self.session_factory() as session:
            LogObservationRepository(session).record(
                incident_id,
                list(capture.records),
                self.clock(),
                source_read_ids=capture.source_read_ids,
            )

    def _diagnose(
        self,
        run_id: str,
        incident: Incident,
        incident_id: UUID,
        correlation_id: UUID,
        alerts: list[Alert],
        trigger: str,
    ) -> Diagnosis:
        # Once resolved, the incident's causal window is frozen at its last
        # transition and no live capture is taken for it. An open incident is
        # captured (objects, Events, lifecycle, then logs), committed, and its
        # single epistemic boundary is the moment that capture finished.
        resolved = incident.status in _TERMINAL_STATUSES
        capture_adapter = ProviderAdapter(
            run_id, "CAPTURE", self.session_factory, self.provider_readers
        )
        window_end = incident.updated_at if resolved else self.clock()
        snapshot_cycle_id: int | None = None
        listed: tuple[dict[str, Any], ...] = ()
        if resolved:
            if self.reader is not None:
                # Keep the shared journal current for later incidents, but do
                # not use this live cycle as evidence for the frozen episode.
                # A reader outage must not make historical diagnosis fail.
                try:
                    self.snapshot_result()
                except Exception:
                    logger.warning(
                        "post-resolution journal refresh failed; using frozen evidence",
                        exc_info=True,
                    )
        else:
            if self.reader is not None:
                cycle = self.snapshot_result(run_id=run_id)
                assert cycle.cycle_id is not None
                snapshot_cycle_id = cycle.cycle_id
                with self.session_factory() as session:
                    listed = SnapshotCycleRepository(session).load(snapshot_cycle_id).objects
            if capture_adapter.supports("logs"):
                self._capture_logs(incident_id, alerts, listed, capture_adapter)
            window_end = self.clock()
        starts_at, ends_at = incident_window(alerts, window_end)
        # The manifest and the run's boundary event commit together; RCA then
        # sees exactly the manifest's members, loaded by id.
        entries = build_manifest(
            self.session_factory,
            ManifestRequest(
                run_id=run_id,
                incident_id=incident_id,
                correlation_id=correlation_id,
                starts_at=starts_at,
                ends_at=ends_at,
                window_end=window_end,
                namespaces=frozenset(self.namespaces),
                journal_namespaces=frozenset((*self.namespaces, *self.evidence_namespaces)),
                snapshot_cycle_id=snapshot_cycle_id,
                listed_objects=len(listed),
                provider_capabilities=capture_adapter.capabilities(),
            ),
            timestamp=self.clock(),
        )
        with self.session_factory() as session:
            members = load_members(session, entries)
        source = LiveSource(
            incident=str(incident_id),
            alert_items=[alert_from_payload(item) for item in members.alerts],
            journal=list(members.journal),
            current_objects=list(members.snapshot.objects) if members.snapshot else [],
            event_bodies=[body for _, body in members.events],
            event_evidence_ids=[event_evidence_id(version_id) for version_id, _ in members.events],
            error_items=list(members.logs),
            observed_at=window_end,
            current_is_live=not resolved,
            provider_adapter=capture_adapter.for_caller("ENGINE"),
            lifecycle_records=members.lifecycle,
            snapshot_cycle_id=members.snapshot.cycle_id if members.snapshot else None,
            snapshot_observed_at=members.snapshot.observed_at if members.snapshot else None,
        )
        bounded_policy = self.bounded_policy_factory()
        # The effective configs are explicit so the revision can record them.
        engine_config = EngineConfig()
        # A product investigation starts from the same source diagnose() reads.
        investigation_config = (
            InvestigationConfig(engine=engine_config, seed_mode=self.investigation_seed_mode)
            if bounded_policy is not None
            else None
        )
        investigation_result = None
        if bounded_policy is not None:
            investigation_result = investigate_diagnosis(
                source, policy=bounded_policy, config=investigation_config
            )
            diagnosis = investigation_result.diagnosis
        else:
            diagnosis = diagnose(
                source, investigator=self.investigator_factory(), config=engine_config
            )
        leading = diagnosis.hypothesis.causal_actor.canonical if diagnosis.hypothesis else None
        self._emit(
            incident_id,
            correlation_id,
            IncidentEventType.RCA_ENGINE_COMPLETED,
            {
                "run_id": run_id,
                "leading_actor": leading,
                "reads": len(diagnosis.steps),
                "evidence": len(diagnosis.evidence),
            },
        )
        # Stamp the stored row with its run id, so the UI can render the timeline
        # of exactly the run that produced this diagnosis rather than inferring it
        # from "latest completed event". The diagnosis stays a pure Diagnosis;
        # the bounded investigation has its own versioned run artifact.
        # Every provider read of the run has been taped by now, so the tape
        # digest is final; the manifest digest is reloaded from its rows.
        created_at = self.clock()
        with self.session_factory() as session:
            manifest_digest = load_manifest_digest(session, run_id)
            tape_digest = load_tape_digest(session, run_id)

            document = diagnosis.model_dump(mode="json")
            # Requirement rows are a pure function of the persisted revision.
            persisted = Diagnosis.model_validate(document)

            def companions(writer: Session, diagnosis_id: int) -> None:
                EvidenceRequirementRepository(writer).apply_revision(
                    incident_id=incident_id, diagnosis_id=diagnosis_id, diagnosis=persisted
                )
                if investigation_result is not None:
                    InvestigationRunRepository(writer).save(
                        diagnosis_run_id=run_id,
                        incident_id=incident_id,
                        artifact_version=TRAJECTORY_ARTIFACT_VERSION,
                        created_at=created_at,
                        document=trajectory_document(investigation_result),
                        commit=False,
                    )

            DiagnosisRepository(session).save_revision(
                incident_id=incident_id,
                document=document,
                created_at=created_at,
                run_id=run_id,
                trigger=trigger,
                window_end=window_end,
                manifest_digest=manifest_digest,
                tape_digest=tape_digest,
                epistemic_digest=diagnosis_epistemic_digest(diagnosis),
                engine_version=engine_version(),
                config_digest=rca_config_digest(engine_config, investigation_config),
                companions=companions,
            )
        self._emit(
            incident_id,
            correlation_id,
            IncidentEventType.DIAGNOSIS_COMPLETED,
            {
                "run_id": run_id,
                "root_cause": diagnosis.root_cause.canonical if diagnosis.root_cause else None,
                "resolution": diagnosis.resolution.value,
                "confidence": diagnosis.confidence.value,
                "model_calls": diagnosis.model_calls,
            },
        )
        return diagnosis


INVESTIGATION_POLICIES = ("deterministic_intent", "deterministic_observation", "llm")


class InvestigationConfigurationError(ValueError):
    """The investigation environment variables do not describe one valid policy."""


def investigation_policy_from_environment(environ: Mapping[str, str]) -> str | None:
    """The configured investigation policy name, or ``None`` when investigation is off.

    Policy selection is separate from model availability: ``SRE_LLM_ENABLED``
    never selects the LLM policy by itself, and choosing ``llm`` without it is
    an error rather than a silent deterministic fallback.
    """
    enabled = environ.get("SRE_INVESTIGATION_ENABLED", "").strip().casefold() or "true"
    if enabled not in {"true", "false"}:
        raise InvestigationConfigurationError(
            f"SRE_INVESTIGATION_ENABLED must be 'true' or 'false', not {enabled!r}"
        )
    if enabled == "false":
        return None
    policy = environ.get("SRE_INVESTIGATION_POLICY", "").strip() or "deterministic_intent"
    if policy not in INVESTIGATION_POLICIES:
        raise InvestigationConfigurationError(
            f"SRE_INVESTIGATION_POLICY must be one of {', '.join(INVESTIGATION_POLICIES)}, "
            f"not {policy!r}"
        )
    if policy == "llm" and environ.get("SRE_LLM_ENABLED", "").casefold() != "true":
        raise InvestigationConfigurationError(
            "SRE_INVESTIGATION_POLICY=llm requires SRE_LLM_ENABLED=true"
        )
    return policy


def service_from_environment(session_factory: sessionmaker[Session]) -> DiagnosisService:
    """Configure cluster access from environment variables; offline by default."""
    investigation_policy = investigation_policy_from_environment(os.environ)
    namespaces = tuple(
        item.strip()
        for item in os.getenv("SRE_WATCH_NAMESPACES", "sre-demo").split(",")
        if item.strip()
    )
    evidence_namespaces = tuple(
        item.strip()
        for item in os.getenv("SRE_EVIDENCE_NAMESPACES", "chaos-mesh").split(",")
        if item.strip()
    )
    reader = (
        KubernetesClusterReader(chaos_namespaces=evidence_namespaces)
        if os.getenv("SRE_CLUSTER_ACCESS") == "true"
        else None
    )
    provider_readers = ProviderReaders.from_environment()

    # Built once and reused: OpenAIClient owns the call-budget counter, so a
    # fresh client per incident would reset SRE_LLM_MAX_CALLS every time.
    llm_client: LLMClient | None = None

    def investigator() -> Investigator | None:
        nonlocal llm_client
        if os.getenv("SRE_LLM_ENABLED", "").casefold() != "true":
            return None
        if llm_client is None:
            from packages.rca.llm import OpenAIClient

            llm_client = OpenAIClient()
        from packages.rca.agent import LLMInvestigator

        return LLMInvestigator(llm_client)

    def bounded_policy() -> InvestigationPolicy | None:
        nonlocal llm_client
        if investigation_policy is None:
            return None
        if investigation_policy == "deterministic_intent":
            return DeterministicIntentPolicy()
        if investigation_policy == "deterministic_observation":
            return DeterministicObservationPolicy()
        if llm_client is None:
            from packages.rca.llm import OpenAIClient

            llm_client = OpenAIClient()
        return LLMInvestigationPolicy(llm_client)

    return DiagnosisService(
        session_factory=session_factory,
        namespaces=namespaces,
        evidence_namespaces=evidence_namespaces,
        reader=reader,
        provider_readers=provider_readers,
        investigator_factory=investigator,
        bounded_policy_factory=bounded_policy,
        status_snapshot_interval=timedelta(
            seconds=float(os.getenv("SRE_STATUS_SNAPSHOT_INTERVAL_SECONDS", "30"))
        ),
        retention_policy=policy_from_environment(),
        reevaluation=reevaluation_from_environment(),
    )


__all__ = [
    "INVESTIGATION_POLICIES",
    "DiagnosisService",
    "InvestigationConfigurationError",
    "investigation_policy_from_environment",
    "service_from_environment",
]
