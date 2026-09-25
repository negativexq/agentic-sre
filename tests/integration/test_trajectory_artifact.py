"""M19-3.15a: 1.1 trajectory artifacts are written with, and loaded by, their replay contract."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture

from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.engine import EngineConfig
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.investigation.state import InvestigationConfig
from packages.rca.model import InvestigationPolicyKind, InvestigationResult
from packages.storage.models import IncidentEventRow, InvestigationRunRow
from packages.storage.repositories import DiagnosisRepository, InvestigationRunRepository
from packages.storage.trajectory import (
    ReplayTrajectoryMalformed,
    ReplayTrajectoryMissing,
    ReplayTrajectoryNotReplayable,
    load_trajectory,
)

BOUNDARY_KEYS = {
    "run_id",
    "window_end",
    "snapshot_cycle_id",
    "objects",
    "journal",
    "events",
    "logs",
    "provider_capabilities",
}


def _recorded_run(world: Any) -> tuple[sessionmaker[Session], Any, str]:
    factory, cluster, clock, incident_id = world
    clock.now = T0 + timedelta(minutes=30)
    DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        clock=clock,
        bounded_policy_factory=lambda: ScriptedInvestigationPolicy(actions=[]),
    ).run(incident_id)
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
    assert run_id is not None
    return factory, incident_id, run_id


def _set_row(factory: sessionmaker[Session], run_id: str, **values: Any) -> None:
    with factory() as session:
        # The artifact is append-only for the ORM; this simulates stored variants.
        session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.diagnosis_run_id == run_id)
            .values(**values)
        )
        session.commit()


def _document(factory: sessionmaker[Session], run_id: str) -> dict[str, Any]:
    with factory() as session:
        row = session.get(InvestigationRunRow, run_id)
        assert row is not None
        return copy.deepcopy(row.document)


def test_a_live_run_writes_a_loadable_1_1_trajectory(setup: Any) -> None:  # noqa: F811
    factory, _, run_id = _recorded_run(setup)
    with factory() as session:
        recorded = load_trajectory(session, run_id)
        boundary = next(
            row.payload
            for row in session.scalars(
                select(IncidentEventRow).where(IncidentEventRow.event_type == "EVIDENCE_GATHERED")
            )
            if row.payload.get("run_id") == run_id
        )
    assert recorded.run_id == run_id
    contract = recorded.contract
    assert contract.policy_kind is InvestigationPolicyKind.ACTION
    assert contract.counts_as_model is False
    assert recorded.config == InvestigationConfig(engine=EngineConfig())
    assert contract.terminal.stop_reason is recorded.result.stop_reason
    assert contract.terminal.audited_turns == len(recorded.result.action_audits)
    # Boundary metadata stays exactly where it was.
    assert set(boundary) == BOUNDARY_KEYS


def test_a_1_0_artifact_stays_readable_but_is_not_trajectory_replayable(
    setup: Any,  # noqa: F811
) -> None:
    factory, _, run_id = _recorded_run(setup)
    legacy = _document(factory, run_id)
    del legacy["replay_contract"]
    _set_row(factory, run_id, artifact_version="1.0", document=legacy)
    with factory() as session:
        artifact = InvestigationRunRepository(session).get(run_id)
        assert artifact is not None and artifact["artifact_version"] == "1.0"
        assert InvestigationResult.model_validate(artifact["document"]).replay_contract is None
        with pytest.raises(ReplayTrajectoryNotReplayable, match="'1.0' is not replayable"):
            load_trajectory(session, run_id)
    _set_row(factory, run_id, artifact_version="2.0")
    with factory() as session, pytest.raises(ReplayTrajectoryNotReplayable):
        load_trajectory(session, run_id)


def test_a_missing_trajectory_is_explicit(setup: Any) -> None:  # noqa: F811
    factory, _, run_id = _recorded_run(setup)
    with factory() as session, pytest.raises(ReplayTrajectoryMissing):
        load_trajectory(session, "another-run")
    with factory() as session:
        assert load_trajectory(session, run_id).run_id == run_id


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda d: {k: v for k, v in d.items() if k != "replay_contract"}, id="1.1-no-contract"
        ),
        pytest.param(lambda d: {**d, "stop_reason": "NOT_A_REASON"}, id="bad-document"),
        pytest.param(
            lambda d: {**d, "replay_contract": {**d["replay_contract"], "policy_kind": "LLM"}},
            id="unknown-kind",
        ),
        pytest.param(
            lambda d: {
                **d,
                "replay_contract": {
                    **d["replay_contract"],
                    "config": {**d["replay_contract"]["config"], "max_turns": "six"},
                },
            },
            id="bad-config",
        ),
        pytest.param(
            lambda d: {
                **d,
                "replay_contract": {
                    **d["replay_contract"],
                    "terminal": {**d["replay_contract"]["terminal"], "audited_turns": 5},
                },
            },
            id="terminal-disagrees",
        ),
        pytest.param(
            lambda d: {
                **d,
                "replay_contract": {
                    **d["replay_contract"],
                    "terminal": {
                        **d["replay_contract"]["terminal"],
                        "stop_reason": "MODEL_FAILURE",
                    },
                },
            },
            id="terminal-reason-disagrees",
        ),
    ],
)
def test_malformed_1_1_trajectories_are_rejected(setup: Any, mutate: Any) -> None:  # noqa: F811
    factory, _, run_id = _recorded_run(setup)
    _set_row(factory, run_id, document=mutate(_document(factory, run_id)))
    with factory() as session, pytest.raises(ReplayTrajectoryMalformed):
        load_trajectory(session, run_id)
