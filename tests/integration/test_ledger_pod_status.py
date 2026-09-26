"""M19-2.6: diagnosis reads Pod status only from persisted lifecycle rows in its window."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture

from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.live import LiveSource
from packages.rca.model import PodStatusObservation
from packages.storage.repositories import LifecycleRepository

POD = "payment-service-7d9f-x2x4q"


def _append(repo: LifecycleRepository, type_: str, minutes: float, status: str) -> str:
    record = repo.append(
        namespace="sre-demo",
        kind="Pod",
        name=POD,
        instance_uid="u-pay",
        type=type_,
        observed_at=T0 + timedelta(minutes=minutes),
        source="collector",
        payload={
            "ready": {
                "type": "Ready",
                "status": status,
                "lastTransitionTime": (T0 + timedelta(minutes=minutes)).isoformat(),
            },
            "containerStatuses": [],
            "deletionTimestamp": None,
        },
    )
    assert record is not None
    return record.evidence_id


def test_every_status_the_diagnosis_sees_is_a_ledger_row_in_its_window(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, cluster, clock, incident_id = setup
    with factory() as session:
        repo = LifecycleRepository(session)
        # The incident window opens two hours before the first alert (T0+11m).
        too_old = _append(repo, "STATUS_SNAPSHOT", 11 - 130, "True")
        in_window = {
            _append(repo, "READY_FALSE", 5, "False"),
            _append(repo, "STATUS_SNAPSHOT", 20, "True"),
        }
        # Other lifecycle types are facts, not status observations.
        repo.append(
            namespace="sre-demo",
            kind="Pod",
            name=POD,
            instance_uid="u-pay",
            type="OBSERVED",
            observed_at=T0 + timedelta(minutes=1),
            source="collector",
            payload={},
        )

    seen: list[Sequence[PodStatusObservation]] = []
    original = LiveSource.pod_status_observations

    def spy(self: LiveSource) -> Sequence[PodStatusObservation]:
        result = original(self)
        seen.append(result)
        return result

    monkeypatch.setattr(LiveSource, "pod_status_observations", spy)
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    clock.now = T0 + timedelta(minutes=30)
    service.run(incident_id, "MANUAL")

    assert seen
    ids = {item.evidence_id for statuses in seen for item in statuses}
    assert ids == in_window
    assert too_old not in ids
    assert not any(item.startswith(("cluster:", "journal:")) for item in ids)
