"""M19-3.16: replaying a recorded investigation trajectory reproduces its epistemic digest."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture
from test_replay_provider import Readers

import packages.rca.investigation.graph as graph_module
import packages.rca.live as live_module
import packages.rca.replay as replay_module
import packages.storage.manifest as storage_manifest_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.epistemic_digest import diagnosis_epistemic_digest
from packages.rca.investigation.intents import DeterministicIntentPolicy
from packages.rca.investigation.policy import LLMIntentPolicy, LLMInvestigationPolicy
from packages.rca.investigation.selection import DeterministicObservationPolicy
from packages.rca.investigation.state import InvestigationPolicyContext
from packages.rca.live import KubernetesClusterReader
from packages.rca.llm import OpenAIClient
from packages.rca.model import InvestigationAction, InvestigationResult
from packages.rca.provider_adapter import ProviderAdapter
from packages.rca.replay import (
    ReplayDivergence,
    ReplayModeUnsupported,
    ReplayProviderAdapter,
    ReplaySource,
    ReplayTrajectoryDivergence,
    replay_run,
    replay_trajectory,
)
from packages.storage.models import (
    AlertRow,
    ChangeRecordRow,
    DiagnosisRow,
    EventVersionRow,
    IncidentEventRow,
    InvestigationReadRow,
    InvestigationRunRow,
    LifecycleObservationRow,
    LogObservationRow,
    ObjectVersionRow,
    RunEvidenceManifestRow,
    SnapshotCycleObjectRow,
    SnapshotCycleRow,
)
from packages.storage.repositories import DiagnosisRepository
from packages.storage.trajectory import ReplayTrajectoryMissing, ReplayTrajectoryNotReplayable

ORDER_RS: dict[str, Any] = {
    "kind": "ReplicaSet",
    "metadata": {
        "name": "order-service-5c9",
        "namespace": "sre-demo",
        "ownerReferences": [{"kind": "Deployment", "name": "order-service"}],
    },
}
ORDER_POD: dict[str, Any] = {
    "kind": "Pod",
    "metadata": {
        "name": "order-service-5c9-abcde",
        "namespace": "sre-demo",
        "uid": "order-uid-1",
        "labels": {"app": "order-service"},
        "ownerReferences": [{"kind": "ReplicaSet", "name": "order-service-5c9"}],
    },
    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
}
PROVIDER_ORDER = ("runtime_traces", "logs", "traffic", "resource_pressure")
TABLES = (
    DiagnosisRow,
    InvestigationRunRow,
    InvestigationReadRow,
    RunEvidenceManifestRow,
    IncidentEventRow,
    SnapshotCycleRow,
    SnapshotCycleObjectRow,
    LogObservationRow,
    LifecycleObservationRow,
    ObjectVersionRow,
    EventVersionRow,
    ChangeRecordRow,
    AlertRow,
)


class ProviderFirstPolicy:
    """A recording ACTION policy: takes the graph's offered candidates, providers first."""

    counts_as_model = False

    def choose_action(self, context: InvestigationPolicyContext) -> InvestigationAction:
        offered = list(context.candidate_actions)
        preferred = sorted(
            (item for item in offered if item.capability in PROVIDER_ORDER),
            key=lambda item: PROVIDER_ORDER.index(item.capability or ""),
        )
        if preferred:
            return preferred[0]
        if offered:
            return offered[0]
        return InvestigationAction(action="stop", rationale="nothing offered")


class Recorded:
    def __init__(
        self,
        factory: sessionmaker[Session],
        incident_id: Any,
        run_id: str,
        readers: Readers,
        original: InvestigationResult,
    ) -> None:
        self.factory = factory
        self.incident_id = incident_id
        self.run_id = run_id
        self.readers = readers
        self.original = original

    @property
    def digest(self) -> str:
        return diagnosis_epistemic_digest(self.original.diagnosis)


def _record(world: Any, readers: Readers, minutes: int = 30) -> Recorded:
    factory, cluster, clock, incident_id = world
    clock.now = T0 + timedelta(minutes=minutes)
    DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        clock=clock,
        provider_readers=readers.configured(),
        bounded_policy_factory=ProviderFirstPolicy,
    ).run(incident_id)
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
        assert run_id is not None
        row = session.get(InvestigationRunRow, run_id)
        assert row is not None
        original = InvestigationResult.model_validate(row.document)
    return Recorded(factory, incident_id, run_id, readers, original)


def _prepare(world: Any) -> None:
    factory, cluster, _, _ = world
    cluster.objects += [copy.deepcopy(ORDER_RS), copy.deepcopy(ORDER_POD)]
    with factory() as session:
        alert = session.scalars(select(AlertRow)).one()
        alert.labels = {"pod": ORDER_POD["metadata"]["name"]}
        session.commit()


@pytest.fixture
def recorded(setup: Any) -> Recorded:  # noqa: F811
    _prepare(setup)
    return _record(setup, Readers())


def _bomb(name: str) -> Any:
    def explode(*_: Any, **__: Any) -> Any:
        raise AssertionError(f"trajectory replay must not call {name}")

    return explode


class _NoClock(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> Any:
        raise AssertionError("trajectory replay must not read the clock")

    @classmethod
    def utcnow(cls) -> Any:
        raise AssertionError("trajectory replay must not read the clock")


def go_offline(monkeypatch: pytest.MonkeyPatch, readers: Readers) -> dict[str, int]:
    """Bomb every live dependency; count replay source/adapter construction."""
    readers.go_offline()
    monkeypatch.setattr(KubernetesClusterReader, "__init__", _bomb("KubernetesClusterReader()"))
    for method in ("_client", "list_objects", "list_events"):
        monkeypatch.setattr(KubernetesClusterReader, method, _bomb(method))
    monkeypatch.setattr(DiagnosisService, "snapshot_result", _bomb("snapshot_result"))
    monkeypatch.setattr(ProviderAdapter, "_provider_call", _bomb("ProviderAdapter._provider_call"))
    monkeypatch.setattr(live_module, "urlopen", _bomb("urlopen"))
    for module in (live_module, replay_module, graph_module, storage_manifest_module):
        monkeypatch.setattr(module, "datetime", _NoClock)
    # No model and no selector may choose anything in trajectory mode.
    monkeypatch.setattr(OpenAIClient, "complete_json", _bomb("OpenAIClient.complete_json"))
    monkeypatch.setattr(LLMInvestigationPolicy, "choose_action", _bomb("LLM choose_action"))
    monkeypatch.setattr(LLMIntentPolicy, "choose_intent", _bomb("LLM choose_intent"))
    for selector in (DeterministicObservationPolicy, DeterministicIntentPolicy):
        monkeypatch.setattr(selector, "choose_action", _bomb(f"{selector.__name__}"))
    for name in (
        "rank_observation_candidates",
        "select_observation_intent_candidate",
        "build_intent_menu",
    ):
        monkeypatch.setattr(graph_module, name, _bomb(f"selector {name}"))
    counts = {"source": 0, "adapter": 0}
    real_source = ReplaySource.from_run.__func__  # type: ignore[attr-defined]
    real_adapter = ReplayProviderAdapter.from_run.__func__  # type: ignore[attr-defined]

    def source_from_run(cls: Any, *args: Any, **kwargs: Any) -> Any:
        counts["source"] += 1
        return real_source(cls, *args, **kwargs)

    def adapter_from_run(cls: Any, *args: Any, **kwargs: Any) -> Any:
        counts["adapter"] += 1
        return real_adapter(cls, *args, **kwargs)

    monkeypatch.setattr(ReplaySource, "from_run", classmethod(source_from_run))
    monkeypatch.setattr(ReplayProviderAdapter, "from_run", classmethod(adapter_from_run))
    return counts


def _snapshot(factory: sessionmaker[Session], run_id: str) -> tuple[Any, ...]:
    with factory() as session:
        counts = tuple(session.scalar(select(func.count()).select_from(table)) for table in TABLES)
        tape = [
            (row.sequence, row.caller_class, row.query_key, row.status, row.observation)
            for row in session.scalars(
                select(InvestigationReadRow)
                .where(InvestigationReadRow.run_id == run_id)
                .order_by(InvestigationReadRow.sequence)
            )
        ]
        artifact = session.get(InvestigationRunRow, run_id)
        assert artifact is not None
        return counts, tape, copy.deepcopy(artifact.document), artifact.artifact_version


def _consumed_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int | None]]:
    consumed: list[tuple[str, int | None]] = []
    real = ReplayProviderAdapter._replay

    def spy(self: ReplayProviderAdapter, *args: Any, **kwargs: Any) -> Any:
        consumed.append((self.caller_class, self.next_sequence))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ReplayProviderAdapter, "_replay", spy)
    return consumed


def test_trajectory_replay_reproduces_the_recorded_digest_offline(
    recorded: Recorded, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(recorded.factory, recorded.run_id)
    tape_callers = [row[1] for row in before[1]]
    # The fixture is non-trivial: a CAPTURE prefix, then provider-backed
    # INVESTIGATION reads interleaved with base-evidence actions.
    assert tape_callers[:3] == ["CAPTURE"] * 3 and tape_callers[3:] == ["INVESTIGATION"] * 2
    audits = recorded.original.action_audits
    assert [audit.action.capability for audit in audits] == [
        "runtime_traces",
        "logs",
        "incident_changes",
        "incident_events",
    ]
    counts = go_offline(monkeypatch, recorded.readers)
    consumed = _consumed_reads(monkeypatch)

    replayed = replay_trajectory(recorded.run_id, session_factory=recorded.factory)

    assert replayed.digest == recorded.digest
    assert counts == {"source": 1, "adapter": 1}
    # One cursor across every turn and caller view: strictly increasing, no reset.
    assert consumed == [("INVESTIGATION", 4), ("INVESTIGATION", 5)]
    assert replayed.source.provider_adapter.next_sequence is None
    assert replayed.policy.exhausted
    assert replayed.policy.consumed_actions == tuple(audit.action for audit in audits)
    assert replayed.result.stop_reason is recorded.original.stop_reason
    assert replayed.result.turns == recorded.original.turns
    assert _snapshot(recorded.factory, recorded.run_id) == before


def test_replay_run_returns_the_digest_and_rejects_other_modes(
    recorded: Recorded, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = go_offline(monkeypatch, recorded.readers)
    assert replay_run(recorded.run_id, session_factory=recorded.factory) == recorded.digest
    assert counts["source"] == 1
    with pytest.raises(ReplayModeUnsupported):
        replay_run(recorded.run_id, "selector", session_factory=recorded.factory)
    assert counts["source"] == 1


def test_a_recorded_provider_error_replays_through_the_normal_path(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(setup)
    readers = Readers()
    readers.tempo.fail = True
    original = _record(setup, readers)
    with original.factory() as session:
        statuses = [
            row.status
            for row in session.scalars(
                select(InvestigationReadRow).where(
                    InvestigationReadRow.run_id == original.run_id,
                    InvestigationReadRow.capability == "tempo_traces",
                )
            )
        ]
    assert statuses == ["ERROR"]
    go_offline(monkeypatch, readers)
    replayed = replay_trajectory(original.run_id, session_factory=original.factory)
    assert replayed.digest == original.digest
    assert replayed.source.provider_adapter.next_sequence is None


def test_runs_replay_only_their_own_trajectory_and_tape(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(setup)
    first = _record(setup, Readers())
    second = _record(setup, Readers(), minutes=35)
    assert first.run_id != second.run_id
    readers = Readers()
    go_offline(monkeypatch, readers)
    for run in (first, second):
        replayed = replay_trajectory(run.run_id, session_factory=run.factory)
        assert replayed.digest == run.digest
        assert replayed.source.run_id == run.run_id
        assert replayed.source.provider_adapter.next_sequence is None


def _rewrite(recorded: Recorded, change: Any) -> None:
    document = copy.deepcopy(recorded.original.model_dump(mode="json"))
    change(document)
    with recorded.factory() as session:
        session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.diagnosis_run_id == recorded.run_id)
            .values(document=document)
        )
        session.commit()


def _terminal(document: dict[str, Any], turns: int) -> None:
    document["turns"] = turns
    terminal = document["replay_contract"]["terminal"]
    terminal["turns"] = turns
    terminal["audited_turns"] = len(document["action_audits"])


def test_a_turn_beyond_the_recorded_trajectory_diverges(
    recorded: Recorded, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drop_last(document: dict[str, Any]) -> None:
        document["action_audits"] = document["action_audits"][:-1]
        _terminal(document, len(document["action_audits"]))

    _rewrite(recorded, drop_last)
    go_offline(monkeypatch, recorded.readers)
    with pytest.raises(ReplayTrajectoryDivergence, match="never took"):
        replay_trajectory(recorded.run_id, session_factory=recorded.factory)


def test_recorded_actions_left_unplayed_diverge(
    recorded: Recorded, monkeypatch: pytest.MonkeyPatch
) -> None:
    def add_one(document: dict[str, Any]) -> None:
        document["action_audits"] = [*document["action_audits"], document["action_audits"][-1]]
        _terminal(document, len(document["action_audits"]))

    _rewrite(recorded, add_one)
    go_offline(monkeypatch, recorded.readers)
    with pytest.raises(ReplayTrajectoryDivergence, match="never replayed"):
        replay_trajectory(recorded.run_id, session_factory=recorded.factory)


def test_a_changed_recorded_action_raises_the_tape_divergence(
    recorded: Recorded, monkeypatch: pytest.MonkeyPatch
) -> None:
    def shift_trace_query(document: dict[str, Any]) -> None:
        query = document["action_audits"][0]["action"]["query"]
        assert query is not None
        query["limit"] = query["limit"] - 1

    _rewrite(recorded, shift_trace_query)
    go_offline(monkeypatch, recorded.readers)
    with pytest.raises(ReplayDivergence):
        replay_trajectory(recorded.run_id, session_factory=recorded.factory)


def test_missing_and_1_0_trajectories_are_not_replayed(
    recorded: Recorded, monkeypatch: pytest.MonkeyPatch
) -> None:
    go_offline(monkeypatch, recorded.readers)
    with pytest.raises(ReplayTrajectoryMissing):
        replay_run("no-such-run", session_factory=recorded.factory)
    with recorded.factory() as session:
        session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.diagnosis_run_id == recorded.run_id)
            .values(artifact_version="1.0")
        )
        session.commit()
    with pytest.raises(ReplayTrajectoryNotReplayable):
        replay_run(recorded.run_id, session_factory=recorded.factory)
