"""Fresh-cluster orchestration of the product-resolution harness (M19-6.7).

``ProductRunner`` owns the order; a ``ProductBackend`` owns the effects. The
same runner drives the live backend (``packages.evals.product.live``) and the
``RecordingBackend`` below, whose trace is therefore the real call order.

Every scenario gets its own cluster, torn down after it: freshness comes from
the environment lifecycle, never from clearing evidence.

An ``ActionVerificationError`` makes the run ``ERROR``: the harness could not
establish or verify the intended test world. It says nothing about the
product's diagnosis. Other failures propagate after teardown.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from packages.evals.product.actions import (
    ActionReceipt,
    ActionVerificationError,
    ClusterControl,
    DeletePodOf,
    DeletionReceipt,
    EvidenceReader,
    IncidentRecord,
    JournalRecord,
    LifecycleRecord,
    PatchService,
    PodIdentity,
    ProductAction,
    ReadinessReceipt,
    SetReadiness,
    SetResources,
)
from packages.evals.product.baseline import DirtyBaseline
from packages.evals.product.spec import Phase, ProductScenario

logger = logging.getLogger(__name__)


class Stage(StrEnum):
    """The frozen fresh-cluster lifecycle, in order."""

    CLUSTER_UP = "CLUSTER_UP"
    DEPLOY_OBSERVABILITY_WORKLOAD = "DEPLOY_OBSERVABILITY_WORKLOAD"
    WAIT_WORKLOAD_READY = "WAIT_WORKLOAD_READY"
    START_FRESH_DB_CONTROL_PLANE = "START_FRESH_DB_CONTROL_PLANE"
    WARMUP = "WARMUP"
    CLEAN_BASELINE = "CLEAN_BASELINE"
    TIMELINE = "TIMELINE"
    AWAIT_R1 = "AWAIT_R1"
    R_EARLY = "R_EARLY"
    AWAIT_R2 = "AWAIT_R2"
    ARTIFACT = "ARTIFACT"
    CLUSTER_DOWN = "CLUSTER_DOWN"


class PreHistoryIncident(RuntimeError):
    """An incident opened before T0: the world was not quiet before the staged root."""

    def __init__(self, incident_ids: Sequence[str], t0: datetime) -> None:
        self.incident_ids = tuple(incident_ids)
        super().__init__(
            f"incident(s) opened before T0 {t0.isoformat()}: {list(self.incident_ids)}"
        )


class RunStatus(StrEnum):
    """Whether the harness established the intended test world (not a verdict)."""

    RUN_OK = "RUN_OK"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class RunResult:
    scenario_id: str
    status: RunStatus
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """DEV timings; the evidence settle bound waits for the collector to persist."""

    warmup: timedelta = timedelta(minutes=10)
    evidence_timeout: timedelta = timedelta(minutes=3)
    evidence_poll: timedelta = timedelta(seconds=5)


class ProductBackend(Protocol):
    """External effects of one run, one method per lifecycle stage."""

    def cluster_up(self) -> None: ...

    def deploy_observability_and_workload(self) -> None: ...

    def wait_workload_ready(self) -> None: ...

    def start_fresh_db_and_control_plane(self) -> None: ...

    def warmup(self, duration: timedelta) -> None: ...

    def clean_baseline(self, scenario: ProductScenario) -> None: ...

    def start_timeline(self, scenario: ProductScenario) -> None: ...

    def set_traffic(self, enabled: bool) -> None: ...

    def control(self) -> ClusterControl: ...

    def evidence(self) -> EvidenceReader: ...

    def await_r1(self, scenario: ProductScenario) -> None: ...

    def run_r_early(self, scenario: ProductScenario) -> None: ...

    def await_r2(self, scenario: ProductScenario) -> None: ...

    def write_artifact(self, scenario: ProductScenario, result: RunResult) -> None: ...

    def cluster_down(self) -> None: ...


# --- evidence convergence ---------------------------------------------------------
# Presence of the last persisted fact each verification needs. Pending evidence
# is waited for (bounded); whether it is *right* is decided only by ``verify``.


def _readiness_settled(
    action: SetReadiness, evidence: EvidenceReader, receipt: ActionReceipt
) -> bool:
    assert isinstance(receipt, ReadinessReceipt)
    return any(
        item.type == "READY_TRUE" and item.observed_at >= receipt.finished_at
        for item in evidence.lifecycle(receipt.uid_before)
    )


def _deletion_settled(
    action: DeletePodOf, evidence: EvidenceReader, receipt: ActionReceipt
) -> bool:
    assert isinstance(receipt, DeletionReceipt)
    old = receipt.old
    tombstone = any(
        item.uid == old.uid
        and item.lifecycle == "DELETED"
        and item.observed_at >= receipt.started_at
        for item in evidence.journal("Pod", old.name)
    )
    deleted = any(
        item.type == "DELETED" and item.observed_at >= receipt.started_at
        for item in evidence.lifecycle(old.uid)
    )
    return tombstone and deleted


def _owned_by(body: Any, deployment: str) -> bool:
    for ref in (body.get("metadata") or {}).get("ownerReferences") or []:
        if ref.get("controller") and (ref.get("kind"), ref.get("name")) == (
            "Deployment",
            deployment,
        ):
            return True
    return False


def _resources_settled(
    action: SetResources, evidence: EvidenceReader, receipt: ActionReceipt
) -> bool:
    return any(
        item.lifecycle == "CREATED"
        and item.observed_at >= receipt.started_at
        and _owned_by(item.body, action.service)
        for item in evidence.journal("ReplicaSet")
    )


def _selector_settled(
    action: PatchService, evidence: EvidenceReader, receipt: ActionReceipt
) -> bool:
    return any(
        item.observed_at >= receipt.started_at
        for item in evidence.journal("Service", action.service)
    )


_SETTLED: dict[type[ProductAction], Callable[[Any, EvidenceReader, ActionReceipt], bool]] = {
    SetReadiness: _readiness_settled,
    DeletePodOf: _deletion_settled,
    SetResources: _resources_settled,
    PatchService: _selector_settled,
}


def await_evidence(
    action: ProductAction,
    receipt: ActionReceipt,
    evidence: EvidenceReader,
    control: ClusterControl,
    config: RunnerConfig,
) -> bool:
    """Poll until the evidence ``verify`` reads is persisted, or the bound passes."""
    settled = next((_SETTLED[kind] for kind in type(action).__mro__ if kind in _SETTLED), None)
    if settled is None:
        raise TypeError(f"no evidence milestone for {type(action).__name__}")
    deadline = control.now() + config.evidence_timeout
    while not settled(action, evidence, receipt):
        if control.now() >= deadline:
            return False
        control.wait(config.evidence_poll)
    return True


# --- orchestration --------------------------------------------------------------------


@dataclass
class ProductRunner:
    backend: ProductBackend
    config: RunnerConfig = field(default_factory=RunnerConfig)
    _traffic_on: bool = field(default=False, init=False, repr=False)

    def run(self, scenario: ProductScenario) -> RunResult:
        """One scenario on one fresh cluster, torn down whatever happens after it exists."""
        self.backend.cluster_up()
        failure: BaseException | None = None
        try:
            return self._staged(scenario)
        except BaseException as error:
            failure = error
            raise
        finally:
            self._traffic_off_best_effort(failure)
            try:
                self.backend.cluster_down()
            except Exception as cleanup:
                if failure is None:
                    raise
                failure.add_note(f"cluster_down also failed: {cleanup!r}")

    def _set_traffic(self, enabled: bool) -> None:
        self.backend.set_traffic(enabled)
        self._traffic_on = enabled

    def _traffic_off_best_effort(self, failure: BaseException | None) -> None:
        if not self._traffic_on:
            return
        try:
            self._set_traffic(False)
        except Exception as error:
            self._traffic_on = False
            if failure is not None:
                failure.add_note(f"traffic off also failed: {error!r}")
            else:
                logger.warning("turning traffic off failed", exc_info=True)

    def run_all(self, scenarios: Iterable[ProductScenario]) -> tuple[RunResult, ...]:
        return tuple(self.run(scenario) for scenario in scenarios)

    def _staged(self, scenario: ProductScenario) -> RunResult:
        backend = self.backend
        backend.deploy_observability_and_workload()
        backend.wait_workload_ready()
        # The collector starts only once the workload is already running.
        backend.start_fresh_db_and_control_plane()
        backend.warmup(self.config.warmup)
        try:
            backend.clean_baseline(scenario)
        except DirtyBaseline as error:
            return RunResult(scenario.scenario_id, RunStatus.ERROR, str(error))
        try:
            self._timeline(scenario)
        except (ActionVerificationError, PreHistoryIncident) as error:
            return RunResult(scenario.scenario_id, RunStatus.ERROR, str(error))
        backend.await_r1(scenario)
        backend.run_r_early(scenario)
        backend.await_r2(scenario)
        self._set_traffic(False)
        result = RunResult(scenario.scenario_id, RunStatus.RUN_OK)
        backend.write_artifact(scenario, result)
        return result

    def _timeline(self, scenario: ProductScenario) -> None:
        """Pre-history with traffic off, the T0 guard, then T0 onwards with traffic on.

        T0 is the start of the timeline minus the earliest negative offset, so
        the first pre-history phase runs now; without pre-history T0 is now.
        """
        backend = self.backend
        backend.start_timeline(scenario)
        control, evidence = backend.control(), backend.evidence()
        self._set_traffic(False)
        earliest = min((phase.offset for phase in scenario.phases), default=timedelta(0))
        t0 = control.now() - min(earliest, timedelta(0))
        for phase in (item for item in scenario.phases if item.offset < timedelta(0)):
            self._phase(phase, t0, control, evidence)
        self._wait_until(t0, control)
        opened = sorted(item.incident_id for item in evidence.incidents() if item.created_at < t0)
        if opened:
            raise PreHistoryIncident(opened, t0)
        self._set_traffic(True)
        for phase in (item for item in scenario.phases if item.offset >= timedelta(0)):
            self._phase(phase, t0, control, evidence)

    @staticmethod
    def _wait_until(when: datetime, control: ClusterControl) -> None:
        remaining = when - control.now()
        if remaining > timedelta(0):
            control.wait(remaining)

    def _phase(
        self, phase: Phase, t0: datetime, control: ClusterControl, evidence: EvidenceReader
    ) -> None:
        self._wait_until(t0 + phase.offset, control)
        for action in phase.actions:
            receipt = action.apply(control)
            await_evidence(action, receipt, evidence, control, self.config)
            action.verify(evidence, receipt)


# --- recording (dry-run) backend ------------------------------------------------------

DRY_RUN_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class _EmptyEvidence:
    """Dry-run evidence: nothing has been observed."""

    def lifecycle(self, instance_uid: str) -> Sequence[LifecycleRecord]:
        return ()

    def journal(self, kind: str, name: str | None = None) -> Sequence[JournalRecord]:
        return ()

    def incidents(self) -> Sequence[IncidentRecord]:
        return ()


class RecordingControl:
    """A virtual clock and a log of intended cluster operations; performs none."""

    def __init__(self, events: list[tuple[Any, ...]], start: datetime = DRY_RUN_EPOCH) -> None:
        self._events = events
        self._now = start
        self._generation: dict[str, int] = {}

    def now(self) -> datetime:
        return self._now

    def wait(self, duration: timedelta) -> None:
        self._events.append(("wait", duration))
        self._now += duration

    def pod_of(self, deployment: str) -> PodIdentity:
        generation = self._generation.setdefault(deployment, 0)
        return PodIdentity(f"{deployment}-{generation}", f"dry-run-{deployment}-{generation}")

    def set_not_ready(self, service: str, not_ready: bool) -> None:
        self._events.append(("set_not_ready", service, not_ready))

    def delete_pod(self, pod: PodIdentity) -> None:
        self._events.append(("delete_pod", pod.name))

    def wait_for_replacement(self, deployment: str, old_uid: str) -> PodIdentity | None:
        self._events.append(("wait_for_replacement", deployment))
        self._generation[deployment] = self._generation.get(deployment, 0) + 1
        return self.pod_of(deployment)

    def patch_resources(self, deployment: str, container: str, limits: Any, requests: Any) -> None:
        self._events.append(
            ("patch_resources", deployment, container, dict(limits), dict(requests))
        )

    def wait_for_rollout(self, deployment: str) -> None:
        self._events.append(("wait_for_rollout", deployment))

    def patch_service_selector(self, service: str, selector: Any) -> None:
        self._events.append(("patch_service_selector", service, dict(selector)))


class RecordingBackend:
    """Dry-run: records each stage and operation in call order, with no effects.

    ``evidence`` defaults to nothing observed, so staged actions cannot verify
    offline; tests pass a fake ledger/journal instead.
    """

    def __init__(self, evidence: EvidenceReader | None = None) -> None:
        self.events: list[tuple[Any, ...]] = []
        self._control = RecordingControl(self.events)
        self._evidence: EvidenceReader = evidence if evidence is not None else _EmptyEvidence()

    def _stage(self, stage: Stage, *detail: Any) -> None:
        self.events.append((stage, *detail))

    def stages(self) -> tuple[Stage, ...]:
        return tuple(event[0] for event in self.events if isinstance(event[0], Stage))

    def cluster_up(self) -> None:
        self._stage(Stage.CLUSTER_UP)

    def deploy_observability_and_workload(self) -> None:
        self._stage(Stage.DEPLOY_OBSERVABILITY_WORKLOAD)

    def wait_workload_ready(self) -> None:
        self._stage(Stage.WAIT_WORKLOAD_READY)

    def start_fresh_db_and_control_plane(self) -> None:
        self._stage(Stage.START_FRESH_DB_CONTROL_PLANE)

    def warmup(self, duration: timedelta) -> None:
        self._stage(Stage.WARMUP, duration)

    def clean_baseline(self, scenario: ProductScenario) -> None:
        self._stage(Stage.CLEAN_BASELINE, scenario.scenario_id)

    def start_timeline(self, scenario: ProductScenario) -> None:
        self._stage(Stage.TIMELINE, tuple(phase.offset for phase in scenario.phases))

    def set_traffic(self, enabled: bool) -> None:
        self.events.append(("set_traffic", enabled))

    def control(self) -> ClusterControl:
        return self._control

    def evidence(self) -> EvidenceReader:
        return self._evidence

    def await_r1(self, scenario: ProductScenario) -> None:
        self._stage(Stage.AWAIT_R1)

    def run_r_early(self, scenario: ProductScenario) -> None:
        self._stage(Stage.R_EARLY)

    def await_r2(self, scenario: ProductScenario) -> None:
        self._stage(Stage.AWAIT_R2)

    def write_artifact(self, scenario: ProductScenario, result: RunResult) -> None:
        self._stage(Stage.ARTIFACT, result.status)

    def cluster_down(self) -> None:
        self._stage(Stage.CLUSTER_DOWN)


__all__ = [
    "DRY_RUN_EPOCH",
    "ProductBackend",
    "PreHistoryIncident",
    "ProductRunner",
    "RecordingBackend",
    "RecordingControl",
    "RunResult",
    "RunStatus",
    "RunnerConfig",
    "Stage",
    "await_evidence",
]
