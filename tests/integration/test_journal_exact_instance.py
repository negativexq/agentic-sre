"""M20.5 P2a: the live object journal keeps exact object instances.

Same namespace/kind/name does not imply the same object instance; a changed observed
UID must survive history compaction.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture

from packages.storage.models import ObjectVersionRow
from packages.storage.repositories import ObjectVersionRepository


def _pod(uid: str | None, resource_version: str = "1") -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": "cart-0",
        "namespace": "sre-demo",
        "resourceVersion": resource_version,
    }
    if uid is not None:
        metadata["uid"] = uid
    return {"kind": "Pod", "metadata": metadata, "spec": {"containers": [{"image": "cart:1"}]}}


def _record(factory: sessionmaker[Session], *bodies: dict[str, Any]) -> list[bool]:
    with factory() as session:
        repository = ObjectVersionRepository(session)
        return [
            repository.record(body, T0 + timedelta(minutes=index))
            for index, body in enumerate(bodies)
        ]


def _rows(factory: sessionmaker[Session]) -> list[tuple[str | None, str]]:
    with factory() as session:
        return [
            (row.uid, row.lifecycle)
            for row in session.scalars(
                select(ObjectVersionRow)
                .where(ObjectVersionRow.object_key == "sre-demo/Pod/cart-0")
                .order_by(ObjectVersionRow.version_id)
            )
        ]


def test_a_new_uid_with_the_same_name_and_body_is_recorded(setup: Any) -> None:  # noqa: F811
    factory = setup[0]
    assert _record(factory, _pod("uid-A"), _pod("uid-B")) == [True, True]
    assert [uid for uid, _ in _rows(factory)] == ["uid-A", "uid-B"]


def test_the_same_instance_and_body_is_not_recorded_again(setup: Any) -> None:  # noqa: F811
    factory = setup[0]
    assert _record(factory, _pod("uid-A", "1"), _pod("uid-A", "2")) == [True, False]
    assert [uid for uid, _ in _rows(factory)] == ["uid-A"]


def test_a_deleted_instance_recreated_under_the_same_name_is_created(setup: Any) -> None:  # noqa: F811
    factory = setup[0]
    assert _record(factory, _pod("uid-A")) == [True]
    with factory() as session:
        assert ObjectVersionRepository(session).tombstone(
            "sre-demo/Pod/cart-0", T0 + timedelta(minutes=5)
        )
    with factory() as session:
        assert ObjectVersionRepository(session).record(_pod("uid-B"), T0 + timedelta(minutes=6))
    assert _rows(factory) == [
        ("uid-A", "OBSERVED"),
        ("uid-A", "DELETED"),
        ("uid-B", "CREATED"),
    ]


def test_an_appearing_or_disappearing_uid_is_not_assumed_the_same_instance(
    setup: Any,  # noqa: F811
) -> None:
    factory = setup[0]
    assert _record(factory, _pod(None), _pod("uid-A"), _pod(None)) == [True, True, True]
    assert [uid for uid, _ in _rows(factory)] == [None, "uid-A", None]
