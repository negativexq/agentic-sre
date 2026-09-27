"""M19-3.17: the recorded deterministic selector, re-run offline, chooses the recorded reads."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture
from test_replay_provider import Readers
from test_trajectory_replay import (
    ProviderFirstPolicy,
    _NoClock,
    _prepare,
    _snapshot,
    go_offline,
)

import packages.rca.investigation.graph as graph_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.policy import ScriptedInvestigationPolicy
from packages.rca.investigation.selection import DeterministicObservationPolicy
from packages.rca.investigation.state import SEED_BOUNDED_INITIAL_VIEW
from packages.rca.model import InvestigationPolicyKind, InvestigationResult
from packages.rca.replay import (
    ReplayDivergence,
    ReplayModeUnsupported,
    ReplayTrajectoryDivergence,
    replay_run,
    replay_selector,
)
from packages.storage.models import InvestigationReadRow, InvestigationRunRow
from packages.storage.repositories import DiagnosisRepository

SELECTORS = [
    pytest.param(
        DeterministicObservationPolicy,
        InvestigationPolicyKind.OBSERVATION_SELECTOR,
        id="observation",
    ),
    pytest.param(DeterministicIntentPolicy, InvestigationPolicyKind.INTENT_SELECTOR, id="intent"),
]


class Run:
    def __init__(
        self,
        factory: sessionmaker[Session],
        run_id: str,
        readers: Readers,
        original: InvestigationResult,
    ) -> None:
        self.factory = factory
        self.run_id = run_id
        self.readers = readers
        self.original = original

    @property
    def digest(self) -> str:
        return diagnosis_epistemic_digest(self.original.diagnosis)


def _record(world: Any, policy: Any, minutes: int = 30) -> Run:
    factory, cluster, clock, incident_id = world
    readers = Readers()
    clock.now = T0 + timedelta(minutes=minutes)
    DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        clock=clock,
        provider_readers=readers.configured(),
        bounded_policy_factory=policy,
        # A non-trivial recorded trajectory exists only on the bounded benchmark seed.
        investigation_seed_mode=SEED_BOUNDED_INITIAL_VIEW,
    ).run(incident_id, "MANUAL")
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
        assert run_id is not None
        row = session.get(InvestigationRunRow, run_id)
        assert row is not None
        original = InvestigationResult.model_validate(row.document)
    return Run(factory, run_id, readers, original)


def _selector_spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count real selector work; trajectory playback must not be used at all."""
    calls = {"observation": 0, "intent": 0}
    for name, key in (
        ("rank_observation_candidates", "observation"),
        ("select_observation_intent_candidate", "intent"),
    ):
        real = getattr(graph_module, name)

        def spy(*args: Any, _real: Any = real, _key: str = key, **kwargs: Any) -> Any:
            calls[_key] += 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(graph_module, name, spy)

    def no_playback(*_: Any, **__: Any) -> Any:
        raise AssertionError("selector replay must not play recorded turns")

    monkeypatch.setattr(graph_module, "_play_recorded_turn", no_playback)
    monkeypatch.setattr(ScriptedInvestigationPolicy, "next_recorded", no_playback)
    return calls


# The real selector entry points, captured before any test patches them.
_SELECTOR_ENTRY_POINTS = {
    name: getattr(graph_module, name)
    for name in (
        "rank_observation_candidates",
        "select_observation_intent_candidate",
        "build_intent_menu",
    )
}
_SELECTOR_CHOOSE = {
    selector: selector.choose_action
    for selector in (DeterministicObservationPolicy, DeterministicIntentPolicy)
}


def _offline_selector(
    monkeypatch: pytest.MonkeyPatch, run: Run
) -> tuple[dict[str, int], dict[str, int]]:
    """Every live dependency bombed as in trajectory mode, but the selector allowed to run."""
    counts = go_offline(monkeypatch, run.readers)
    for name, real in _SELECTOR_ENTRY_POINTS.items():
        monkeypatch.setattr(graph_module, name, real)
    for selector, choose in _SELECTOR_CHOOSE.items():
        monkeypatch.setattr(selector, "choose_action", choose)
    return counts, _selector_spies(monkeypatch)


@pytest.mark.parametrize(("policy", "kind"), SELECTORS)
def test_selector_replay_selects_the_recorded_reads_and_digest(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    policy: Any,
    kind: InvestigationPolicyKind,
) -> None:
    _prepare(setup)
    run = _record(setup, policy)
    assert run.original.replay_contract is not None
    assert run.original.replay_contract.policy_kind is kind
    before = _snapshot(run.factory, run.run_id)
    assert [row[1] for row in before[1]] == ["CAPTURE"] * 3 + ["INVESTIGATION"]
    counts, selector_calls = _offline_selector(monkeypatch, run)

    replayed = replay_selector(run.run_id, session_factory=run.factory)

    assert replayed.digest == run.digest
    assert replayed.recorded_query_keys == replayed.replayed_query_keys
    assert replayed.recorded_query_keys == tuple(row[2] for row in before[1][3:])
    assert replayed.source.provider_adapter.next_sequence is None
    assert type(replayed.policy) is policy
    assert [audit.action for audit in replayed.result.action_audits] == [
        audit.action for audit in run.original.action_audits
    ]
    assert (replayed.result.stop_reason, replayed.result.turns) == (
        run.original.stop_reason,
        run.original.turns,
    )
    assert counts == {"source": 1, "adapter": 1}
    key = "observation" if kind is InvestigationPolicyKind.OBSERVATION_SELECTOR else "intent"
    assert selector_calls[key] > 0  # the selector really chose again
    assert _snapshot(run.factory, run.run_id) == before


def test_replay_run_selector_mode_returns_the_digest(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(setup)
    run = _record(setup, DeterministicObservationPolicy)
    _offline_selector(monkeypatch, run)
    assert replay_run(run.run_id, "selector", session_factory=run.factory) == run.digest


def test_selector_mode_rejects_trajectories_a_selector_did_not_choose(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(setup)
    action_run = _record(setup, ProviderFirstPolicy)
    assert action_run.original.replay_contract is not None
    assert action_run.original.replay_contract.policy_kind is InvestigationPolicyKind.ACTION
    _offline_selector(monkeypatch, action_run)
    with pytest.raises(ReplayModeUnsupported, match="ACTION"):
        replay_run(action_run.run_id, "selector", session_factory=action_run.factory)
    _rewrite(action_run, lambda d: d["replay_contract"].update(policy_kind="INTENT_TIEBREAK"))
    with pytest.raises(ReplayModeUnsupported, match="INTENT_TIEBREAK"):
        replay_selector(action_run.run_id, session_factory=action_run.factory)


def _rewrite(run: Run, change: Any) -> None:
    document = copy.deepcopy(run.original.model_dump(mode="json"))
    change(document)
    with run.factory() as session:
        session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.diagnosis_run_id == run.run_id)
            .values(document=document)
        )
        session.commit()


def test_a_different_recorded_config_makes_the_selector_diverge(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(setup)
    run = _record(setup, DeterministicObservationPolicy)
    _rewrite(run, lambda d: d["replay_contract"]["config"].update(max_turns=1))
    _offline_selector(monkeypatch, run)
    with pytest.raises(ReplayTrajectoryDivergence):
        replay_selector(run.run_id, session_factory=run.factory)


def test_a_changed_tape_key_makes_the_selector_diverge(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(setup)
    run = _record(setup, DeterministicObservationPolicy)
    with run.factory() as session:
        session.execute(
            update(InvestigationReadRow)
            .where(
                InvestigationReadRow.run_id == run.run_id,
                InvestigationReadRow.caller_class == "INVESTIGATION",
            )
            .values(query_key="some-other-read")
        )
        session.commit()
    _offline_selector(monkeypatch, run)
    with pytest.raises(ReplayDivergence):
        replay_selector(run.run_id, session_factory=run.factory)


def test_a_recorded_wall_time_stop_is_reproduced_without_the_clock(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(setup)
    run = _record(setup, DeterministicObservationPolicy)

    def wall_time_after_first_turn(document: dict[str, Any]) -> None:
        document["action_audits"] = document["action_audits"][:1]
        document["turns"] = 1
        document["stop_reason"] = "WALL_TIME_EXHAUSTED"
        document["replay_contract"]["terminal"] = {
            "stop_reason": "WALL_TIME_EXHAUSTED",
            "turns": 1,
            "audited_turns": 1,
        }

    _rewrite(run, wall_time_after_first_turn)
    _offline_selector(monkeypatch, run)
    assert getattr(graph_module, "datetime") is _NoClock  # noqa: B009 - any clock read raises
    replayed = replay_selector(run.run_id, session_factory=run.factory)
    assert replayed.result.stop_reason.value == "WALL_TIME_EXHAUSTED"
    assert replayed.result.turns == 1
    assert replayed.source.provider_adapter.next_sequence is None


def test_selector_replay_is_deterministic_across_processes(
    setup: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    _prepare(setup)
    runs = {
        "observation": _record(setup, DeterministicObservationPolicy),
        "intent": _record(setup, DeterministicIntentPolicy, minutes=35),
    }
    expected = {name: [run.run_id, run.digest] for name, run in runs.items()}
    database = setup[0]().get_bind().url.database
    script = (
        "import json, sys\n"
        "from sqlalchemy import create_engine\n"
        "from packages.storage.database import create_session_factory\n"
        "from packages.rca.replay import replay_selector\n"
        "factory = create_session_factory(create_engine(sys.argv[1]))\n"
        "print(json.dumps({name: replay_selector(run_id, session_factory=factory).digest\n"
        "                  for name, (run_id, _) in json.loads(sys.argv[2]).items()}))\n"
    )
    for seed in ("11", "12"):
        completed = subprocess.run(
            [sys.executable, "-c", script, f"sqlite:///{database}", json.dumps(expected)],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=Path(__file__).resolve().parents[2],
        )
        digests = json.loads(completed.stdout.strip().splitlines()[-1])
        assert digests == {name: digest for name, (_, digest) in expected.items()}
