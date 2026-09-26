"""M19-6.6: verified scenario actions against a fake ledger, journal and control port.

Every positive case spells out the evidence that makes it pass; the fakes
never generate evidence on their own.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.evals.product import Phase, ProductScenario
from packages.evals.product.actions import (
    ActionReceipt,
    ActionVerificationError,
    DeletePodOf,
    DeletionReceipt,
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
from packages.evals.product.spec import Expectation

T = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T + timedelta(seconds=seconds)


# --- fakes ------------------------------------------------------------------------


class FakeEvidence:
    """Read-only evidence exactly as a test declares it; records every read."""

    def __init__(
        self,
        lifecycle: Sequence[LifecycleRecord] = (),
        journal: Sequence[JournalRecord] = (),
        incidents: Sequence[IncidentRecord] = (),
    ) -> None:
        self._lifecycle = tuple(lifecycle)
        self._journal = tuple(journal)
        self._incidents = tuple(incidents)
        self.reads: list[tuple[str, ...]] = []

    def lifecycle(self, instance_uid: str) -> Sequence[LifecycleRecord]:
        self.reads.append(("lifecycle", instance_uid))
        return [item for item in self._lifecycle if item.instance_uid == instance_uid]

    def journal(self, kind: str, name: str | None = None) -> Sequence[JournalRecord]:
        self.reads.append(("journal", kind, name or "*"))
        return [
            item
            for item in self._journal
            if item.kind == kind and (name is None or item.name == name)
        ]

    def incidents(self) -> Sequence[IncidentRecord]:
        self.reads.append(("incidents",))
        return self._incidents

    def snapshot(self) -> tuple[Any, ...]:
        return (self._lifecycle, self._journal, self._incidents)


class FakeControl:
    """Scripted live reads, a deterministic clock, and a log of every call."""

    def __init__(
        self,
        pods: Sequence[PodIdentity] = (),
        replacement: PodIdentity | None = None,
        start: datetime = T,
    ) -> None:
        self._pods = list(pods)
        self._replacement = replacement
        self._now = start
        self.calls: list[tuple[Any, ...]] = []

    def now(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now

    def wait(self, duration: timedelta) -> None:
        self.calls.append(("wait", duration))
        self._now += duration

    def pod_of(self, deployment: str) -> PodIdentity:
        self.calls.append(("pod_of", deployment))
        return self._pods.pop(0)

    def set_not_ready(self, service: str, not_ready: bool) -> None:
        self.calls.append(("set_not_ready", service, not_ready))

    def delete_pod(self, pod: PodIdentity) -> None:
        self.calls.append(("delete_pod", pod.name, pod.uid))

    def wait_for_replacement(self, deployment: str, old_uid: str) -> PodIdentity | None:
        self.calls.append(("wait_for_replacement", deployment, old_uid))
        return self._replacement

    def patch_resources(
        self,
        deployment: str,
        container: str,
        limits: Mapping[str, str],
        requests: Mapping[str, str],
    ) -> None:
        self.calls.append(("patch_resources", deployment, container, dict(limits), dict(requests)))

    def wait_for_rollout(self, deployment: str) -> None:
        self.calls.append(("wait_for_rollout", deployment))

    def patch_service_selector(self, service: str, selector: Mapping[str, str]) -> None:
        self.calls.append(("patch_service_selector", service, dict(selector)))


def ledger(uid: str, type_: str, seconds: float, restarts: int = 0) -> LifecycleRecord:
    return LifecycleRecord(
        instance_uid=uid,
        type=type_,
        observed_at=at(seconds),
        source_at=None,
        payload={"containerStatuses": [{"name": "order-service", "restartCount": restarts}]},
    )


def version(
    kind: str,
    name: str,
    seconds: float,
    body: Mapping[str, Any],
    *,
    uid: str | None = None,
    lifecycle: str = "UPDATED",
) -> JournalRecord:
    return JournalRecord(kind, name, uid, at(seconds), lifecycle, body)


# --- common -----------------------------------------------------------------------

ACTIONS = [
    SetReadiness("order-service", True, timedelta(seconds=40)),
    DeletePodOf("payment-service"),
    SetResources("order-service", limits={"cpu": "200m"}),
    PatchService("order-service", {"app": "order-service"}),
]


@pytest.mark.parametrize("action", ACTIONS, ids=lambda item: type(item).__name__)
def test_every_action_is_a_frozen_product_action(action: ProductAction) -> None:
    assert isinstance(action, ProductAction)
    assert type(action).apply is not ProductAction.apply
    assert type(action).verify is not ProductAction.verify
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(action, "service", "other")  # noqa: B010 - frozen dataclass must refuse


def test_actions_fit_the_scenario_phases() -> None:
    scenario = ProductScenario(
        scenario_id="actions",
        phases=(Phase(timedelta(minutes=-3), tuple(ACTIONS)),),
        expectation=Expectation(),
    )
    assert scenario.phases[0].actions == tuple(ACTIONS)


def test_mappings_are_copied_frozen_and_ordered() -> None:
    limits = {"memory": "128Mi", "cpu": "200m"}
    selector = {"tier": "web", "app": "order-service"}
    resources = SetResources("order-service", limits=limits)
    patch = PatchService("order-service", selector)
    limits["cpu"] = "999m"
    selector["app"] = "changed"
    assert list(resources.limits.items()) == [("cpu", "200m"), ("memory", "128Mi")]
    assert list(patch.selector.items()) == [("app", "order-service"), ("tier", "web")]
    with pytest.raises(TypeError):
        resources.limits["cpu"] = "1"  # type: ignore[index]
    assert resources.container == "order-service"
    assert SetResources("order-service", limits={"cpu": "200m"}) == SetResources(
        "order-service", limits={"cpu": "200m"}
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda: SetReadiness("", True, timedelta(seconds=1)),
        lambda: SetReadiness("order-service", True, timedelta(seconds=-1)),
        lambda: SetReadiness("order-service", True, 40),  # type: ignore[arg-type]
        lambda: SetReadiness("order-service", False, timedelta(seconds=1)),
        lambda: DeletePodOf(" "),
        lambda: SetResources("order-service"),
        lambda: SetResources("order-service", limits={"gpu": "1"}),
        lambda: SetResources("order-service", limits={"cpu": ""}),
        lambda: SetResources("order-service", limits={"cpu": "1"}, container=""),
        lambda: PatchService("order-service", {}),
        lambda: PatchService("order-service", {"app": ""}),
        lambda: PatchService("", {"app": "x"}),
    ],
)
def test_malformed_declarations_fail_before_any_call(build: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        build()


def test_zero_duration_is_a_valid_declaration() -> None:
    assert SetReadiness("order-service", True, timedelta(0)).duration == timedelta(0)


def test_a_receipt_of_another_action_is_a_verification_error() -> None:
    with pytest.raises(ActionVerificationError, match="receipt of another action"):
        DeletePodOf("x").verify(FakeEvidence(), ActionReceipt(at(0), at(1)))


# --- SetReadiness ---------------------------------------------------------------

READINESS = SetReadiness("order-service", True, timedelta(seconds=40))
READY_RECEIPT = ReadinessReceipt(at(10), at(50), "u1", "u1")


def _readiness_evidence(
    *,
    uid: str = "u1",
    records: Sequence[LifecycleRecord] | None = None,
    incidents: Sequence[IncidentRecord] = (),
) -> FakeEvidence:
    return FakeEvidence(
        lifecycle=records
        if records is not None
        else (
            ledger(uid, "STATUS_SNAPSHOT", 0),  # baseline before the action
            ledger(uid, "READY_FALSE", 20),
            ledger(uid, "READY_TRUE", 55),
        ),
        incidents=incidents,
    )


def test_readiness_passes_on_same_uid_false_then_true_no_restart_no_incident() -> None:
    READINESS.verify(_readiness_evidence(), READY_RECEIPT)


@pytest.mark.parametrize(
    ("evidence", "receipt", "invariant"),
    [
        pytest.param(
            _readiness_evidence(),
            ReadinessReceipt(at(10), at(50), "u1", "u2"),
            "Pod replaced",
            id="uid-changed",
        ),
        pytest.param(
            _readiness_evidence(
                records=(ledger("u1", "STATUS_SNAPSHOT", 0), ledger("u1", "READY_TRUE", 55))
            ),
            READY_RECEIPT,
            "no READY_FALSE",
            id="ready-false-missing",
        ),
        pytest.param(
            _readiness_evidence(
                records=(ledger("u1", "STATUS_SNAPSHOT", 0), ledger("u1", "READY_FALSE", 20))
            ),
            READY_RECEIPT,
            "no READY_TRUE",
            id="ready-true-missing",
        ),
        pytest.param(
            _readiness_evidence(
                records=(
                    ledger("u1", "STATUS_SNAPSHOT", 0),
                    ledger("u1", "READY_TRUE", 55),
                    ledger("u1", "READY_FALSE", 60),
                )
            ),
            READY_RECEIPT,
            "no READY_TRUE",
            id="order-reversed",
        ),
        pytest.param(
            _readiness_evidence(
                records=(
                    ledger("u1", "STATUS_SNAPSHOT", 0),
                    ledger("u2", "READY_FALSE", 20),
                    ledger("u2", "READY_TRUE", 55),
                )
            ),
            READY_RECEIPT,
            "no READY_FALSE",
            id="other-uid-evidence",
        ),
        pytest.param(
            _readiness_evidence(
                records=(
                    ledger("u1", "STATUS_SNAPSHOT", 0, restarts=0),
                    ledger("u1", "READY_FALSE", 20, restarts=0),
                    ledger("u1", "READY_TRUE", 55, restarts=1),
                )
            ),
            READY_RECEIPT,
            "restartCount changed",
            id="restarted",
        ),
        pytest.param(
            _readiness_evidence(
                records=(ledger("u1", "READY_FALSE", 20), ledger("u1", "READY_TRUE", 55))
            ),
            READY_RECEIPT,
            "no ledger baseline",
            id="no-baseline",
        ),
        pytest.param(
            _readiness_evidence(incidents=(IncidentRecord("i1", at(30)),)),
            READY_RECEIPT,
            "incident opened",
            id="incident-inside",
        ),
        pytest.param(
            _readiness_evidence(incidents=(IncidentRecord("i1", at(10)),)),
            READY_RECEIPT,
            "incident opened",
            id="incident-at-start-boundary",
        ),
        pytest.param(
            _readiness_evidence(incidents=(IncidentRecord("i1", at(50)),)),
            READY_RECEIPT,
            "incident opened",
            id="incident-at-end-boundary",
        ),
    ],
)
def test_readiness_rejects_each_broken_invariant(
    evidence: FakeEvidence, receipt: ReadinessReceipt, invariant: str
) -> None:
    with pytest.raises(ActionVerificationError, match=invariant) as raised:
        READINESS.verify(evidence, receipt)
    assert raised.value.action == "SetReadiness"


def test_readiness_ignores_incidents_outside_the_interval() -> None:
    evidence = _readiness_evidence(
        incidents=(IncidentRecord("before", at(9)), IncidentRecord("after", at(51)))
    )
    READINESS.verify(evidence, READY_RECEIPT)


def test_readiness_apply_toggles_the_fault_around_the_wait() -> None:
    control = FakeControl(pods=[PodIdentity("order-a", "u1"), PodIdentity("order-a", "u1")])
    receipt = READINESS.apply(control)
    assert control.calls == [
        ("pod_of", "order-service"),
        ("set_not_ready", "order-service", True),
        ("wait", timedelta(seconds=40)),
        ("set_not_ready", "order-service", False),
        ("pod_of", "order-service"),
    ]
    assert isinstance(receipt, ReadinessReceipt)
    assert (receipt.uid_before, receipt.uid_after) == ("u1", "u1")
    assert receipt.finished_at - receipt.started_at == timedelta(seconds=41)


# --- DeletePodOf ----------------------------------------------------------------

DELETE = DeletePodOf("payment-service")
OLD, NEW = PodIdentity("payment-a", "u1"), PodIdentity("payment-b", "u2")
DELETE_RECEIPT = DeletionReceipt(at(10), at(40), OLD, NEW)
SPEC = {"replicas": 1, "template": {"spec": {"containers": [{"name": "payment-service"}]}}}


def _deletion_evidence(
    *,
    tombstone_uid: str | None = "u1",
    ledger_uid: str | None = "u1",
    spec_after: Mapping[str, Any] = SPEC,
) -> FakeEvidence:
    journal = [
        version("Deployment", "payment-service", 0, {"spec": SPEC}),
        version("Deployment", "payment-service", 30, {"spec": spec_after, "status": {"ready": 1}}),
    ]
    if tombstone_uid is not None:
        journal.append(version("Pod", "payment-a", 20, {}, uid=tombstone_uid, lifecycle="DELETED"))
    lifecycle = [ledger(ledger_uid, "DELETED", 20)] if ledger_uid is not None else []
    return FakeEvidence(lifecycle=lifecycle, journal=journal)


def test_deletion_passes_on_tombstone_deleted_new_uid_and_same_spec() -> None:
    DELETE.verify(_deletion_evidence(), DELETE_RECEIPT)


@pytest.mark.parametrize(
    ("evidence", "receipt", "invariant"),
    [
        pytest.param(
            _deletion_evidence(tombstone_uid=None),
            DELETE_RECEIPT,
            "no journal tombstone",
            id="no-tombstone",
        ),
        pytest.param(
            _deletion_evidence(ledger_uid=None), DELETE_RECEIPT, "no DELETED", id="no-deleted"
        ),
        pytest.param(
            _deletion_evidence(tombstone_uid="u9", ledger_uid="u9"),
            DELETE_RECEIPT,
            "no journal tombstone",
            id="other-uid-deleted",
        ),
        pytest.param(
            _deletion_evidence(ledger_uid="u9"),
            DELETE_RECEIPT,
            "no DELETED",
            id="deleted-on-other-uid",
        ),
        pytest.param(
            FakeEvidence(
                lifecycle=[ledger("u1", "DELETED", 20)],
                journal=[version("Pod", "payment-a", 20, {}, uid="u1", lifecycle="DELETED")],
            ),
            DELETE_RECEIPT,
            "no journal baseline",
            id="no-deployment-baseline",
        ),
        pytest.param(
            _deletion_evidence(),
            DeletionReceipt(at(10), at(40), OLD, None),
            "no replacement Pod",
            id="no-replacement",
        ),
        pytest.param(
            _deletion_evidence(),
            DeletionReceipt(at(10), at(40), OLD, PodIdentity("payment-a", "u1")),
            "replacement has the old UID",
            id="same-uid",
        ),
        pytest.param(
            _deletion_evidence(spec_after={**SPEC, "replicas": 2}),
            DELETE_RECEIPT,
            "Deployment spec changed",
            id="spec-changed",
        ),
    ],
)
def test_deletion_rejects_each_broken_invariant(
    evidence: FakeEvidence, receipt: DeletionReceipt, invariant: str
) -> None:
    with pytest.raises(ActionVerificationError, match=invariant):
        DELETE.verify(evidence, receipt)


def test_a_tombstone_from_before_the_action_does_not_count() -> None:
    evidence = FakeEvidence(
        lifecycle=[ledger("u1", "DELETED", 20)],
        journal=[
            version("Deployment", "payment-service", 0, {"spec": SPEC}),
            version("Pod", "payment-a", 5, {}, uid="u1", lifecycle="DELETED"),
        ],
    )
    with pytest.raises(ActionVerificationError, match="no journal tombstone"):
        DELETE.verify(evidence, DELETE_RECEIPT)


def test_deletion_apply_deletes_exactly_the_captured_pod() -> None:
    control = FakeControl(pods=[OLD], replacement=NEW)
    receipt = DELETE.apply(control)
    assert control.calls == [
        ("pod_of", "payment-service"),
        ("delete_pod", "payment-a", "u1"),
        ("wait_for_replacement", "payment-service", "u1"),
    ]
    assert isinstance(receipt, DeletionReceipt)
    assert (receipt.old, receipt.replacement) == (OLD, NEW)


# --- SetResources ---------------------------------------------------------------

RESOURCES = SetResources("order-service", limits={"cpu": "200m"})
WINDOW = ActionReceipt(at(10), at(40))


def _deployment(cpu: str, *, image: str = "order:1", replicas: int = 1) -> dict[str, Any]:
    return {
        "spec": {
            "replicas": replicas,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "order-service",
                            "image": image,
                            "resources": {"limits": {"cpu": cpu, "memory": "512Mi"}},
                        }
                    ]
                }
            },
        }
    }


def _replica_set(cpu: str, owner: str = "order-service") -> dict[str, Any]:
    return {
        "metadata": {
            "ownerReferences": [{"kind": "Deployment", "name": owner, "controller": True}]
        },
        **_deployment(cpu),
    }


def _resources_evidence(
    *,
    after: Mapping[str, Any] | None = None,
    target: str = "order-service",
    rollout: Mapping[str, Any] | None = None,
    change_at: float = 20,
) -> FakeEvidence:
    journal = [version("Deployment", "order-service", 0, _deployment("500m"))]
    if after is not None or target != "order-service":
        journal.append(version("Deployment", target, change_at, after or _deployment("200m")))
    else:
        journal.append(version("Deployment", "order-service", change_at, _deployment("200m")))
    if rollout is not None:
        journal.append(version("ReplicaSet", "order-service-new", 25, rollout, lifecycle="CREATED"))
    return FakeEvidence(journal=journal)


def test_resources_pass_on_resource_only_diff_and_new_replica_set() -> None:
    RESOURCES.verify(_resources_evidence(rollout=_replica_set("200m")), WINDOW)


def test_quantities_compare_by_value() -> None:
    evidence = _resources_evidence(after=_deployment("0.2"), rollout=_replica_set("0.2"))
    RESOURCES.verify(evidence, WINDOW)


@pytest.mark.parametrize(
    ("evidence", "invariant"),
    [
        pytest.param(
            FakeEvidence(journal=[version("Deployment", "order-service", 0, _deployment("500m"))]),
            "no journal change",
            id="no-change",
        ),
        pytest.param(
            _resources_evidence(
                after=_deployment("500m", replicas=2), rollout=_replica_set("500m")
            ),
            "changes beyond resources",
            id="only-unrelated-change",
        ),
        pytest.param(
            _resources_evidence(
                after=_deployment("200m", image="order:2"), rollout=_replica_set("200m")
            ),
            "changes beyond resources",
            id="extra-image-change",
        ),
        pytest.param(
            _resources_evidence(
                after={
                    "spec": {
                        **_deployment("200m")["spec"],
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "order-service",
                                        "image": "order:1",
                                        "resources": {"limits": {"cpu": "200m", "memory": "256Mi"}},
                                    }
                                ]
                            }
                        },
                    }
                },
                rollout=_replica_set("200m"),
            ),
            "changes beyond resources",
            id="unrequested-resource-field",
        ),
        pytest.param(
            _resources_evidence(target="payment-service", rollout=_replica_set("200m")),
            "no journal change",
            id="wrong-target",
        ),
        pytest.param(_resources_evidence(), "no new ReplicaSet", id="no-rollout"),
        pytest.param(
            _resources_evidence(rollout=_replica_set("200m", owner="payment-service")),
            "no new ReplicaSet",
            id="rollout-of-other-deployment",
        ),
        pytest.param(
            _resources_evidence(change_at=5, rollout=_replica_set("200m")),
            "no journal change",
            id="historical-change-only",
        ),
    ],
)
def test_resources_reject_each_broken_invariant(evidence: FakeEvidence, invariant: str) -> None:
    with pytest.raises(ActionVerificationError, match=invariant):
        RESOURCES.verify(evidence, WINDOW)


def test_an_older_matching_change_does_not_satisfy_a_later_action() -> None:
    """The same cpu change journaled before the action is history, not this action."""
    evidence = FakeEvidence(
        journal=[
            version("Deployment", "order-service", 0, _deployment("500m")),
            version("Deployment", "order-service", 5, _deployment("200m")),
            version(
                "ReplicaSet", "order-service-new", 6, _replica_set("200m"), lifecycle="CREATED"
            ),
        ]
    )
    with pytest.raises(ActionVerificationError, match="no journal change"):
        RESOURCES.verify(evidence, WINDOW)


def test_resources_apply_patches_only_the_container_resources() -> None:
    control = FakeControl()
    receipt = SetResources("order-service", limits={"memory": "128Mi"}).apply(control)
    assert control.calls == [
        ("patch_resources", "order-service", "order-service", {"memory": "128Mi"}, {}),
        ("wait_for_rollout", "order-service"),
    ]
    assert receipt.finished_at > receipt.started_at


# --- PatchService ---------------------------------------------------------------

PATCH = PatchService("order-service", {"app": "payment-service"})


def _service(
    selector: Mapping[str, str], *, port: int = 8000, service_type: str = "ClusterIP"
) -> dict[str, Any]:
    return {
        "metadata": {"annotations": {"note": "x"}},
        "spec": {"selector": dict(selector), "ports": [{"port": port}], "type": service_type},
    }


def _selector_evidence(
    after: Mapping[str, Any] | None, *, name: str = "order-service", seconds: float = 20
) -> FakeEvidence:
    journal = [version("Service", "order-service", 0, _service({"app": "order-service"}))]
    if after is not None:
        journal.append(version("Service", name, seconds, after))
    return FakeEvidence(journal=journal)


def test_selector_passes_on_selector_only_diff() -> None:
    PATCH.verify(_selector_evidence(_service({"app": "payment-service"})), WINDOW)


def test_annotation_changes_are_not_spec_changes() -> None:
    changed = _service({"app": "payment-service"})
    changed["metadata"] = {
        "annotations": {"note": "y", "kubectl.kubernetes.io/last-applied-configuration": "{}"}
    }
    PATCH.verify(_selector_evidence(changed), WINDOW)


@pytest.mark.parametrize(
    ("evidence", "invariant"),
    [
        pytest.param(_selector_evidence(None), "no journal change", id="missing"),
        pytest.param(
            _selector_evidence(_service({"app": "payment-service"}), name="payment-service"),
            "no journal change",
            id="wrong-service",
        ),
        pytest.param(
            _selector_evidence(_service({"app": "order-service"}, port=9000)),
            "changes beyond the selector",
            id="selector-unchanged-port-changed",
        ),
        pytest.param(
            _selector_evidence(_service({"app": "payment-service", "tier": "web"})),
            "selector is not the requested one",
            id="extra-selector-key",
        ),
        pytest.param(
            _selector_evidence(_service({"app": "payment-service"}, service_type="NodePort")),
            "changes beyond the selector",
            id="type-changed",
        ),
        pytest.param(
            _selector_evidence(_service({"app": "payment-service"}), seconds=5),
            "no journal change",
            id="historical-change-only",
        ),
    ],
)
def test_selector_rejects_each_broken_invariant(evidence: FakeEvidence, invariant: str) -> None:
    with pytest.raises(ActionVerificationError, match=invariant):
        PATCH.verify(evidence, WINDOW)


def test_selector_apply_patches_only_the_service_selector() -> None:
    control = FakeControl()
    PATCH.apply(control)
    assert control.calls == [
        ("patch_service_selector", "order-service", {"app": "payment-service"})
    ]


# --- read-only, deterministic, never manufactured -------------------------------------


@pytest.mark.parametrize(
    ("action", "evidence", "receipt"),
    [
        (READINESS, _readiness_evidence(), READY_RECEIPT),
        (DELETE, _deletion_evidence(), DELETE_RECEIPT),
        (RESOURCES, _resources_evidence(rollout=_replica_set("200m")), WINDOW),
        (PATCH, _selector_evidence(_service({"app": "payment-service"})), WINDOW),
    ],
    ids=["readiness", "deletion", "resources", "selector"],
)
def test_verify_is_read_only_and_repeatable(
    action: ProductAction, evidence: FakeEvidence, receipt: ActionReceipt
) -> None:
    before = copy.deepcopy(evidence.snapshot())
    action.verify(evidence, receipt)
    first = list(evidence.reads)
    action.verify(evidence, receipt)
    assert evidence.reads == first + first  # same reads, same outcome
    assert evidence.snapshot() == before


@pytest.mark.parametrize(
    ("action", "control"),
    [
        (READINESS, FakeControl(pods=[PodIdentity("o", "u1"), PodIdentity("o", "u1")])),
        (DELETE, FakeControl(pods=[OLD], replacement=NEW)),
        (RESOURCES, FakeControl()),
        (PATCH, FakeControl()),
    ],
    ids=["readiness", "deletion", "resources", "selector"],
)
def test_apply_never_touches_evidence(action: ProductAction, control: FakeControl) -> None:
    evidence = FakeEvidence()
    action.apply(control)
    assert evidence.reads == [] and evidence.snapshot() == ((), (), ())
    allowed = {
        "wait",
        "pod_of",
        "set_not_ready",
        "delete_pod",
        "wait_for_replacement",
        "patch_resources",
        "wait_for_rollout",
        "patch_service_selector",
    }
    assert {call[0] for call in control.calls} <= allowed
