"""M20.3a: a replay re-runs the RCA engine, so it refuses runs recorded under other semantics."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture
from test_replay_provider import Readers
from test_trajectory_replay import _prepare

import packages.rca.engine as engine_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.evals.product import proof_inputs
from packages.evals.product.proof import _replay_divergence
from packages.rca.engine import RCA_ENGINE_VERSION
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.state import SEED_BOUNDED_INITIAL_VIEW
from packages.rca.replay import ReplayEngineIncompatible, ReplayModeUnsupported, replay_run
from packages.storage.models import DiagnosisRow
from packages.storage.repositories import DiagnosisRepository


def _record(world: Any, policy: Any = None) -> tuple[sessionmaker[Session], str, DiagnosisRow]:
    _prepare(world)
    factory, cluster, clock, incident_id = world
    clock.now = T0 + timedelta(minutes=30)
    kwargs: dict[str, Any] = {}
    if policy is not None:
        kwargs.update(
            bounded_policy_factory=policy, investigation_seed_mode=SEED_BOUNDED_INITIAL_VIEW
        )
    DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        clock=clock,
        provider_readers=Readers().configured(),
        **kwargs,
    ).run(incident_id, "MANUAL")
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
        assert run_id is not None
        row = session.scalars(select(DiagnosisRow).where(DiagnosisRow.run_id == run_id)).one()
        session.expunge(row)
    return factory, run_id, row


def _set_engine(factory: sessionmaker[Session], run_id: str, version: str | None) -> None:
    with factory() as session:
        session.execute(
            update(DiagnosisRow).where(DiagnosisRow.run_id == run_id).values(engine_version=version)
        )
        session.commit()


RUNS = [
    pytest.param(None, ("base",), id="normal-run"),
    pytest.param(DeterministicIntentPolicy, ("trajectory", "selector"), id="investigation-run"),
]


def test_a_live_run_records_the_rca_engine_version(setup: Any) -> None:  # noqa: F811
    _, _, row = _record(setup)
    assert row.engine_version == RCA_ENGINE_VERSION


@pytest.mark.parametrize(("policy", "modes"), RUNS)
def test_the_recording_engine_replays(setup: Any, policy: Any, modes: tuple[str, ...]) -> None:  # noqa: F811
    factory, run_id, row = _record(setup, policy)
    for mode in modes:
        assert replay_run(run_id, mode, session_factory=factory) == row.epistemic_digest


@pytest.mark.parametrize(("policy", "modes"), RUNS)
@pytest.mark.parametrize(
    ("recorded", "message"),
    [
        pytest.param("0.0.1", "RCA engine 0.0.1", id="other-version"),
        pytest.param(None, "no RCA engine version", id="missing-version"),
    ],
)
def test_another_or_unknown_engine_version_is_unsupported(
    setup: Any,  # noqa: F811
    policy: Any,
    modes: tuple[str, ...],
    recorded: str | None,
    message: str,
) -> None:
    factory, run_id, _ = _record(setup, policy)
    _set_engine(factory, run_id, recorded)
    for mode in modes:
        with pytest.raises(ReplayEngineIncompatible, match=message):
            replay_run(run_id, mode, session_factory=factory)


@pytest.mark.parametrize(("policy", "modes"), RUNS)
def test_a_later_engine_refuses_runs_recorded_before_it(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    policy: Any,
    modes: tuple[str, ...],
) -> None:
    """After an RCA rule change bumps the version, old runs are UNSUPPORTED, not DIVERGED."""
    factory, run_id, _ = _record(setup, policy)
    monkeypatch.setattr(engine_module, "RCA_ENGINE_VERSION", "99.0.0")
    for mode in modes:
        with pytest.raises(ReplayEngineIncompatible, match="99.0.0"):
            replay_run(run_id, mode, session_factory=factory)


def test_an_engine_mismatch_is_unsupported_with_an_explicit_reason(setup: Any) -> None:  # noqa: F811
    factory, run_id, row = _record(setup)
    _set_engine(factory, run_id, "0.0.1")
    assert issubclass(ReplayEngineIncompatible, ReplayModeUnsupported)
    facts = proof_inputs.revision_facts(row.diagnosis_id, factory)
    assert facts.replay_status == "UNSUPPORTED"
    assert facts.replay_reason is not None and "RCA engine 0.0.1" in facts.replay_reason
    reasons: dict[str, str] = {}
    assert _replay_divergence((facts,), reasons) is None
    assert "RCA engine 0.0.1" in reasons["replay_reason"]
    assert reasons["replay_divergence"] == "not every revision was replayed"
