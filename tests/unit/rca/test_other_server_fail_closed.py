"""m21 §34: the only server is shown by the journal's replicas, never by a pod the ledger does not hold."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.model import EntityRef, Lifecycle, ObjectVersion, PodStatusObservation
from packages.rca.service_effect import other_server_ready

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
START, END = T0 + timedelta(minutes=5), T0 + timedelta(minutes=12)
NS = "shop"
TARGET = "payment-a-1"


def ref(kind: str, name: str) -> EntityRef:
    return EntityRef(kind=kind, name=name, namespace=NS)


def version(
    kind: str, name: str, uid: str, owner: str | None, minute: float = 0, deleted: bool = False
) -> ObjectVersion:
    metadata: dict[str, object] = {"uid": uid, "name": name}
    if owner:
        metadata["ownerReferences"] = [{"uid": owner, "controller": True}]
    return ObjectVersion(
        entity=ref(kind, name),
        observed_at=T0 + timedelta(minutes=minute),
        body={"metadata": metadata},
        evidence_id=f"object:{uid}:{minute}",
        lifecycle=Lifecycle.DELETED if deleted else Lifecycle.CREATED,
    )


def journal(*pods: ObjectVersion) -> dict[EntityRef, list[ObjectVersion]]:
    history: dict[EntityRef, list[ObjectVersion]] = {
        ref("Deployment", "payment"): [version("Deployment", "payment", "d1", None)],
        ref("ReplicaSet", "payment-a"): [version("ReplicaSet", "payment-a", "rs1", "d1")],
        ref("ReplicaSet", "payment-b"): [version("ReplicaSet", "payment-b", "rs2", "d1")],
    }
    for pod in (version("Pod", TARGET, "p1", "rs1"), *pods):
        history.setdefault(pod.entity, []).append(pod)
    return history


def status(pod: str, minute: float, ready: bool | None) -> PodStatusObservation:
    at = T0 + timedelta(minutes=minute)
    return PodStatusObservation(
        pod=ref("Pod", pod),
        observed_at=at,
        ready=ready,
        ready_since=at,
        evidence_id=f"s:{pod}:{minute}",
    )


TARGET_SEEN = [status(TARGET, 4, True)]


def test_a_journal_holding_only_the_target_shows_it_was_the_only_server() -> None:
    assert other_server_ready(TARGET_SEEN, TARGET, NS, START, END, journal()) is False


def test_without_the_journal_the_only_server_is_unknown() -> None:
    assert other_server_ready(TARGET_SEEN, TARGET, NS, START, END) is None
    assert other_server_ready(TARGET_SEEN, TARGET, NS, START, END, {}) is None


def test_a_replica_without_a_readiness_record_is_unknown_not_unready() -> None:
    history = journal(version("Pod", "payment-a-2", "p2", "rs1"))
    assert other_server_ready(TARGET_SEEN, TARGET, NS, START, END, history) is None


def test_a_replica_of_another_revision_counts_by_ownership_not_by_name() -> None:
    history = journal(version("Pod", "unrelated-name", "p3", "rs2"))
    assert other_server_ready(TARGET_SEEN, TARGET, NS, START, END, history) is None
    seen = [*TARGET_SEEN, status("unrelated-name", 6, True)]
    assert other_server_ready(seen, TARGET, NS, START, END, history) is True


def test_a_replica_known_unready_at_the_start_leaves_the_target_the_only_server() -> None:
    history = journal(version("Pod", "payment-a-2", "p2", "rs1"))
    statuses = [*TARGET_SEEN, status("payment-a-2", 3, False)]
    assert other_server_ready(statuses, TARGET, NS, START, END, history) is False


def test_an_unknown_state_during_the_interval_is_unknown() -> None:
    history = journal(version("Pod", "payment-a-2", "p2", "rs1"))
    statuses = [*TARGET_SEEN, status("payment-a-2", 3, False), status("payment-a-2", 8, None)]
    assert other_server_ready(statuses, TARGET, NS, START, END, history) is None


def test_a_replica_gone_before_the_interval_does_not_count() -> None:
    history = journal(
        version("Pod", "payment-a-2", "p2", "rs1", minute=0),
        version("Pod", "payment-a-2", "p2", "rs1", minute=2, deleted=True),
    )
    assert other_server_ready(TARGET_SEEN, TARGET, NS, START, END, history) is False


def test_a_pod_of_another_workload_with_a_similar_name_does_not_count() -> None:
    history = journal(version("Pod", "payment-a-9", "p9", "other-rs"))
    assert other_server_ready(TARGET_SEEN, TARGET, NS, START, END, history) is False


def test_a_target_the_ledger_does_not_hold_is_unknown() -> None:
    assert other_server_ready([], TARGET, NS, START, END, journal()) is None
