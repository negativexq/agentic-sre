"""M21 amendment 4 P4 gates: same boundary → same onset → same digest; 1.2.2 runs unsupported."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from test_live_diagnosis import T0, cover_alert_channel, setup  # noqa: F401 - pytest fixture

from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.engine import RCA_ENGINE_VERSION, EngineConfig, build_case
from packages.rca.replay import ReplayEngineIncompatible, ReplaySource, replay_run
from packages.rca.signals import ONSET_ANCHORED
from packages.storage.models import DiagnosisRow
from packages.storage.repositories import DiagnosisRepository


def _covered_run(world: Any) -> tuple[str, DiagnosisRow]:
    factory, cluster, clock, incident_id = world
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    service.snapshot()
    clock.now = T0 + timedelta(minutes=13)
    cover_alert_channel(factory, start=T0, until=clock.now)
    service.run(incident_id, "MANUAL")
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
        assert run_id is not None
        row = session.scalars(select(DiagnosisRow).where(DiagnosisRow.run_id == run_id)).one()
        session.expunge(row)
    return run_id, row


def test_the_frozen_boundary_replays_the_same_onset_and_digest(setup: Any) -> None:  # noqa: F811
    factory, *_ = setup
    run_id, row = _covered_run(setup)
    assert row.engine_version == RCA_ENGINE_VERSION == "2.12.0"
    assert row.document["symptoms"]["onset_basis"] == ONSET_ANCHORED
    replayed = build_case(ReplaySource.from_run(run_id, session_factory=factory), EngineConfig())
    assert replayed.symptoms.onset is not None
    assert replayed.symptoms.onset.isoformat() == row.document["symptoms"]["onset"].replace(
        "Z", "+00:00"
    )
    assert replay_run(run_id, "base", session_factory=factory) == row.epistemic_digest


@pytest.mark.parametrize("recorded_version", ["1.2.2", "2.11.0"])
def test_an_older_engine_run_is_not_silently_reinterpreted(
    setup: Any,  # noqa: F811
    recorded_version: str,
) -> None:
    factory, *_ = setup
    run_id, _ = _covered_run(setup)
    with factory() as session:
        session.execute(
            update(DiagnosisRow)
            .where(DiagnosisRow.run_id == run_id)
            .values(engine_version=recorded_version)
        )
        session.commit()
    with pytest.raises(ReplayEngineIncompatible, match=recorded_version):
        replay_run(run_id, "base", session_factory=factory)
