"""change_view marks the leading actor by kind and name, not name alone."""

from __future__ import annotations

from datetime import UTC, datetime

from apps.control_plane.console.mappers import change_view
from packages.contracts import ChangeRecord, ChangeScope, ChangeType
from packages.rca.model import EntityRef

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _record(kind: str, name: str) -> ChangeRecord:
    return ChangeRecord(
        timestamp=T0,
        resource_type=kind,
        resource_name=name,
        change_type=ChangeType.UPDATED,
        scope=ChangeScope.DEPLOYMENT,
        before={},
        after={},
        revision="1",
        source="harness",
    )


def test_matches_when_kind_and_name_agree() -> None:
    leading = EntityRef(kind="Deployment", name="payment-service", namespace="sre-demo")
    view = change_view(_record("Deployment", "payment-service"), T0, leading)
    assert view.matches_leading_actor is True


def test_same_name_different_kind_is_not_flagged() -> None:
    leading = EntityRef(kind="Deployment", name="payment-service", namespace="sre-demo")
    view = change_view(_record("Service", "payment-service"), T0, leading)
    assert view.matches_leading_actor is False


def test_no_leading_actor_never_flags() -> None:
    view = change_view(_record("Deployment", "payment-service"), T0, None)
    assert view.matches_leading_actor is False
