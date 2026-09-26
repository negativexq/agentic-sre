"""Verified scenario actions of the product-resolution harness (M19-6.6).

Each action has two halves:

* ``apply(control)`` performs one intended change through a control port and
  returns an immutable receipt of what it observed while doing so. It never
  receives an evidence handle, so it cannot write evidence.
* ``verify(evidence, receipt)`` reads persisted evidence through a read-only
  port: the lifecycle ledger (``lifecycle_observations``), the object journal
  (``object_versions``) and incidents. It returns normally or raises
  ``ActionVerificationError``.

M19-6.7 MUST translate ``ActionVerificationError`` into a product run
``ERROR``; it is an execution failure, not an RCA or scenario result.

The records below mirror the persisted rows' fields; the live ports of M19-6.7
fill them from storage and the Kubernetes API.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Protocol

# --- persisted evidence, as the read port returns it -------------------------------


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    """One ``lifecycle_observations`` row of one exact Pod instance."""

    instance_uid: str
    type: str
    observed_at: datetime
    source_at: datetime | None
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """One ``object_versions`` row; ``lifecycle`` DELETED is a tombstone."""

    kind: str
    name: str
    uid: str | None
    observed_at: datetime
    lifecycle: str
    body: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    """One incident; ``created_at`` is when the control plane opened it."""

    incident_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PodIdentity:
    """The exact Pod instance a live read found."""

    name: str
    uid: str


class EvidenceReader(Protocol):
    """Read-only access to persisted evidence of the scenario namespace."""

    def lifecycle(self, instance_uid: str) -> Sequence[LifecycleRecord]: ...

    def journal(self, kind: str, name: str | None = None) -> Sequence[JournalRecord]: ...

    def incidents(self) -> Sequence[IncidentRecord]: ...


class ClusterControl(Protocol):
    """The live operations actions may perform, plus time."""

    def now(self) -> datetime: ...

    def wait(self, duration: timedelta) -> None: ...

    def pod_of(self, deployment: str) -> PodIdentity: ...

    def set_not_ready(self, service: str, not_ready: bool) -> None: ...

    def delete_pod(self, pod: PodIdentity) -> None: ...

    def wait_for_replacement(self, deployment: str, old_uid: str) -> PodIdentity | None: ...

    def patch_resources(
        self,
        deployment: str,
        container: str,
        limits: Mapping[str, str],
        requests: Mapping[str, str],
    ) -> None: ...

    def wait_for_rollout(self, deployment: str) -> None: ...

    def patch_service_selector(self, service: str, selector: Mapping[str, str]) -> None: ...


# --- receipts and the failure boundary --------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    """What ``apply`` observed; ``verify`` scopes evidence to ``[started_at, …)``."""

    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class ReadinessReceipt(ActionReceipt):
    uid_before: str
    uid_after: str


@dataclass(frozen=True, slots=True)
class DeletionReceipt(ActionReceipt):
    old: PodIdentity
    replacement: PodIdentity | None


class ActionVerificationError(RuntimeError):
    """A staged action's intended effect is not what the evidence shows."""

    def __init__(self, action: str, invariant: str, **details: Any) -> None:
        self.action = action
        self.invariant = invariant
        self.details = dict(details)
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(self.details.items()))
        super().__init__(f"{action}: {invariant}" + (f" ({rendered})" if rendered else ""))


class ProductAction:
    """Base of every staged scenario action."""

    __slots__ = ()

    def apply(self, control: ClusterControl) -> ActionReceipt:
        raise NotImplementedError

    def verify(self, evidence: EvidenceReader, receipt: ActionReceipt) -> None:
        raise NotImplementedError


# --- helpers ------------------------------------------------------------------------


def _name(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")
    return value


def _frozen(mapping: Mapping[str, str], what: str) -> Mapping[str, str]:
    if not isinstance(mapping, Mapping):
        raise TypeError(f"{what} must be a mapping")
    for key, value in mapping.items():
        _name(key, f"{what} key")
        _name(value, f"{what}[{key}]")
    return MappingProxyType(dict(sorted(mapping.items())))


def _receipt(receipt: ActionReceipt, expected: type[ActionReceipt], action: str) -> Any:
    if not isinstance(receipt, expected):
        raise ActionVerificationError(action, "receipt of another action")
    return receipt


def _restart_counts(record: LifecycleRecord) -> dict[str, Any]:
    return {
        str(item.get("name")): item.get("restartCount")
        for item in record.payload.get("containerStatuses") or []
        if isinstance(item, Mapping)
    }


def _spec(record: JournalRecord) -> Any:
    return record.body.get("spec")


def _spec_changes(before: JournalRecord, after: JournalRecord) -> list[tuple[str, Any, Any]]:
    """Changed desired-state leaves, in the journal diff notation RCA uses."""
    from packages.rca.signals import _diff  # noqa: PLC0415 - keeps listing free of RCA

    return _diff(_spec(before), _spec(after), ".spec")


def _same_quantity(left: Any, right: str) -> bool:
    from packages.rca.signals import parse_quantity  # noqa: PLC0415

    if not isinstance(left, str):
        return False
    a, b = parse_quantity(left), parse_quantity(right)
    return left == right if a is None or b is None else a == b


def _scoped(
    records: Sequence[JournalRecord], started_at: datetime, action: str, target: str
) -> tuple[JournalRecord, list[JournalRecord]]:
    """The target's last version before the action and its versions since."""
    ordered = sorted(records, key=lambda item: item.observed_at)
    before = [item for item in ordered if item.observed_at < started_at]
    if not before:
        raise ActionVerificationError(
            action, "no journal baseline before the action", target=target
        )
    return before[-1], [item for item in ordered if item.observed_at >= started_at]


def _controller(body: Mapping[str, Any]) -> tuple[Any, Any] | None:
    for ref in (body.get("metadata") or {}).get("ownerReferences") or []:
        if isinstance(ref, Mapping) and ref.get("controller"):
            return ref.get("kind"), ref.get("name")
    return None


def _container(body: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    template = ((body.get("spec") or {}).get("template") or {}).get("spec") or {}
    for item in template.get("containers") or []:
        if isinstance(item, Mapping) and item.get("name") == name:
            return item
    return {}


# --- actions ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetReadiness(ProductAction):
    """Take ``service`` out of readiness for ``duration`` without restarting it."""

    service: str
    not_ready: bool
    duration: timedelta

    def __post_init__(self) -> None:
        _name(self.service, "SetReadiness.service")
        if self.not_ready is not True:
            raise ValueError("SetReadiness supports only a transient readiness loss")
        if not isinstance(self.duration, timedelta) or self.duration < timedelta(0):
            raise ValueError("SetReadiness.duration must be a non-negative timedelta")

    def apply(self, control: ClusterControl) -> ReadinessReceipt:
        before = control.pod_of(self.service)
        started_at = control.now()
        control.set_not_ready(self.service, True)
        control.wait(self.duration)
        control.set_not_ready(self.service, False)
        finished_at = control.now()
        after = control.pod_of(self.service)
        return ReadinessReceipt(started_at, finished_at, before.uid, after.uid)

    def verify(self, evidence: EvidenceReader, receipt: ActionReceipt) -> None:
        action = "SetReadiness"
        seen: ReadinessReceipt = _receipt(receipt, ReadinessReceipt, action)
        uid = seen.uid_before
        if seen.uid_after != uid:
            raise ActionVerificationError(action, "Pod replaced", before=uid, after=seen.uid_after)
        own = sorted(
            (item for item in evidence.lifecycle(uid) if item.instance_uid == uid),
            key=lambda item: item.observed_at,
        )
        went_false = next(
            (
                item
                for item in own
                if item.type == "READY_FALSE" and item.observed_at >= seen.started_at
            ),
            None,
        )
        if went_false is None:
            raise ActionVerificationError(action, "no READY_FALSE in the ledger", uid=uid)
        came_back = next(
            (
                item
                for item in own
                if item.type == "READY_TRUE"
                and item.observed_at > went_false.observed_at
                and item.observed_at >= seen.finished_at
            ),
            None,
        )
        if came_back is None:
            raise ActionVerificationError(action, "no READY_TRUE after READY_FALSE", uid=uid)
        baseline = [item for item in own if item.observed_at < seen.started_at]
        if not baseline:
            raise ActionVerificationError(action, "no ledger baseline before the action", uid=uid)
        before, after = _restart_counts(baseline[-1]), _restart_counts(came_back)
        if not before or before != after:
            raise ActionVerificationError(
                action, "restartCount changed", uid=uid, before=before, after=after
            )
        opened = [
            item.incident_id
            for item in evidence.incidents()
            if seen.started_at <= item.created_at <= seen.finished_at
        ]
        if opened:
            raise ActionVerificationError(action, "incident opened", incidents=sorted(opened))


@dataclass(frozen=True, slots=True)
class DeletePodOf(ProductAction):
    """Delete the Pod of ``service``'s Deployment and let it be replaced."""

    service: str

    def __post_init__(self) -> None:
        _name(self.service, "DeletePodOf.service")

    def apply(self, control: ClusterControl) -> DeletionReceipt:
        old = control.pod_of(self.service)
        started_at = control.now()
        control.delete_pod(old)
        replacement = control.wait_for_replacement(self.service, old.uid)
        return DeletionReceipt(started_at, control.now(), old, replacement)

    def verify(self, evidence: EvidenceReader, receipt: ActionReceipt) -> None:
        action = "DeletePodOf"
        seen: DeletionReceipt = _receipt(receipt, DeletionReceipt, action)
        old = seen.old
        tombstoned = any(
            item.uid == old.uid
            and item.lifecycle == "DELETED"
            and item.observed_at >= seen.started_at
            for item in evidence.journal("Pod", old.name)
        )
        if not tombstoned:
            raise ActionVerificationError(action, "no journal tombstone", uid=old.uid)
        deleted = any(
            item.instance_uid == old.uid
            and item.type == "DELETED"
            and item.observed_at >= seen.started_at
            for item in evidence.lifecycle(old.uid)
        )
        if not deleted:
            raise ActionVerificationError(action, "no DELETED in the ledger", uid=old.uid)
        if seen.replacement is None:
            raise ActionVerificationError(action, "no replacement Pod", uid=old.uid)
        if seen.replacement.uid == old.uid:
            raise ActionVerificationError(action, "replacement has the old UID", uid=old.uid)
        baseline, since = _scoped(
            evidence.journal("Deployment", self.service), seen.started_at, action, self.service
        )
        for item in since:
            if _spec(item) != _spec(baseline):
                raise ActionVerificationError(
                    action,
                    "Deployment spec changed",
                    paths=[path for path, _, _ in _spec_changes(baseline, item)],
                )


_RESOURCE_NAMES = frozenset({"cpu", "memory"})


@dataclass(frozen=True, slots=True)
class SetResources(ProductAction):
    """Change only the given CPU/memory limits and requests of one container."""

    service: str
    limits: Mapping[str, str] = field(default_factory=dict)
    requests: Mapping[str, str] = field(default_factory=dict)
    container: str | None = None

    def __post_init__(self) -> None:
        _name(self.service, "SetResources.service")
        object.__setattr__(self, "limits", _frozen(self.limits, "SetResources.limits"))
        object.__setattr__(self, "requests", _frozen(self.requests, "SetResources.requests"))
        if not self.limits and not self.requests:
            raise ValueError("SetResources needs at least one limit or request")
        unknown = (set(self.limits) | set(self.requests)) - _RESOURCE_NAMES
        if unknown:
            raise ValueError(f"SetResources changes only cpu/memory, not {sorted(unknown)}")
        container = self.service if self.container is None else self.container
        object.__setattr__(self, "container", _name(container, "SetResources.container"))

    def _target(self) -> str:
        assert self.container is not None
        return self.container

    def _paths(self) -> dict[str, str]:
        prefix = f".spec.template.spec.containers[{self._target()}].resources"
        return {
            f"{prefix}.{section}.{name}": value
            for section, values in (("limits", self.limits), ("requests", self.requests))
            for name, value in values.items()
        }

    def apply(self, control: ClusterControl) -> ActionReceipt:
        started_at = control.now()
        control.patch_resources(self.service, self._target(), self.limits, self.requests)
        control.wait_for_rollout(self.service)
        return ActionReceipt(started_at, control.now())

    def verify(self, evidence: EvidenceReader, receipt: ActionReceipt) -> None:
        action = "SetResources"
        seen: ActionReceipt = _receipt(receipt, ActionReceipt, action)
        wanted = self._paths()
        baseline, since = _scoped(
            evidence.journal("Deployment", self.service), seen.started_at, action, self.service
        )
        if not since:
            raise ActionVerificationError(action, "no journal change", target=self.service)
        for item in since:
            extra = [path for path, _, _ in _spec_changes(baseline, item) if path not in wanted]
            if extra:
                raise ActionVerificationError(action, "changes beyond resources", paths=extra)
        final = {path: new for path, _, new in _spec_changes(baseline, since[-1])}
        missing = [
            path
            for path, value in wanted.items()
            if path not in final or not _same_quantity(final[path], value)
        ]
        if missing:
            raise ActionVerificationError(action, "intended resources not applied", paths=missing)
        resources = _container(since[-1].body, self._target()).get("resources")
        rolled = any(
            item.lifecycle == "CREATED"
            and item.observed_at >= seen.started_at
            and _controller(item.body) == ("Deployment", self.service)
            and _container(item.body, self._target()).get("resources") == resources
            for item in evidence.journal("ReplicaSet")
        )
        if not rolled:
            raise ActionVerificationError(
                action, "no new ReplicaSet rolled out", target=self.service
            )


@dataclass(frozen=True, slots=True)
class PatchService(ProductAction):
    """Set a Service's selector to exactly ``selector``, changing nothing else."""

    service: str
    selector: Mapping[str, str]

    def __post_init__(self) -> None:
        _name(self.service, "PatchService.service")
        object.__setattr__(self, "selector", _frozen(self.selector, "PatchService.selector"))
        if not self.selector:
            raise ValueError("PatchService.selector must not be empty")

    def apply(self, control: ClusterControl) -> ActionReceipt:
        started_at = control.now()
        control.patch_service_selector(self.service, self.selector)
        return ActionReceipt(started_at, control.now())

    def verify(self, evidence: EvidenceReader, receipt: ActionReceipt) -> None:
        action = "PatchService"
        seen: ActionReceipt = _receipt(receipt, ActionReceipt, action)
        baseline, since = _scoped(
            evidence.journal("Service", self.service), seen.started_at, action, self.service
        )
        if not since:
            raise ActionVerificationError(action, "no journal change", target=self.service)
        for item in since:
            extra = [
                path
                for path, _, _ in _spec_changes(baseline, item)
                if not path.startswith(".spec.selector.")
            ]
            if extra:
                raise ActionVerificationError(action, "changes beyond the selector", paths=extra)
        final = (_spec(since[-1]) or {}).get("selector")
        if final != dict(self.selector):
            raise ActionVerificationError(
                action, "selector is not the requested one", selector=final
            )


__all__ = [
    "ActionReceipt",
    "ActionVerificationError",
    "ClusterControl",
    "DeletePodOf",
    "DeletionReceipt",
    "EvidenceReader",
    "IncidentRecord",
    "JournalRecord",
    "LifecycleRecord",
    "PatchService",
    "PodIdentity",
    "ProductAction",
    "ReadinessReceipt",
    "SetReadiness",
    "SetResources",
]
