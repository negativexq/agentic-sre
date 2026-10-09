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


def test_namespace_is_projected_from_explicit_snapshot_metadata() -> None:
    record = _record("Deployment", "payment-service").model_copy(
        update={
            "after": {
                "kind": "Deployment",
                "metadata": {"name": "payment-service", "namespace": "production"},
            }
        }
    )
    assert change_view(record).namespace == "production"


def test_missing_conflicting_or_mismatched_namespace_is_not_inferred() -> None:
    record = _record("Deployment", "payment-service")
    assert change_view(record).namespace is None
    conflicting = record.model_copy(
        update={
            "before": {"metadata": {"namespace": "one"}},
            "after": {"metadata": {"namespace": "two"}},
        }
    )
    assert change_view(conflicting).namespace is None
    for payload in [
        {"metadata": {"name": "other", "namespace": "production"}},
        {"kind": "Service", "metadata": {"namespace": "production"}},
        {"metadata": {"namespace": 123}},
    ]:
        assert change_view(record.model_copy(update={"after": payload})).namespace is None
