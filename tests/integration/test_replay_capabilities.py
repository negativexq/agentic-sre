"""M19-3.14a: a run's provider capability contract is frozen in its boundary for replay."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from test_evidence_manifest import POD as _POD
from test_evidence_manifest import Seen
from test_live_diagnosis import T0, FakeLogs, setup  # noqa: F401 - pytest fixture

from apps.control_plane.diagnosis import DiagnosisService
from packages.rca.live import LiveSource
from packages.rca.model import EntityRef, InvestigationQuery
from packages.rca.provider_adapter import PROVIDER_CAPABILITIES, ProviderReaders
from packages.rca.replay import ReplaySource
from packages.storage.manifest import (
    ReplayDataError,
    ReplayProviderCapabilitiesMissing,
    canonical_provider_capabilities,
)
from packages.storage.models import IncidentEventRow, InvestigationReadRow
from packages.storage.repositories import DiagnosisRepository

POD: dict[str, Any] = _POD
# Every name either source is asked about: base, provider-backed, alias and unknown.
ASKED = (
    *PROVIDER_CAPABILITIES,
    "history",
    "events",
    "incident_events",
    "incident_changes",
    "tempo_traces",
    "loki_logs",
    "unknown",
)


class FakePrometheus:
    def query_resource_pressure(self, target: EntityRef, query: InvestigationQuery) -> Any:
        return ()

    def query_traffic(self, target: EntityRef, query: InvestigationQuery) -> Any:
        return ()


class FakeTempo:
    def query(self, target: EntityRef, query: InvestigationQuery) -> Any:
        return ()


READERS: dict[str, tuple[Callable[[], ProviderReaders], list[str]]] = {
    "none": (lambda: ProviderReaders(), []),
    "loki": (lambda: ProviderReaders(loki=FakeLogs([])), ["logs"]),
    "prometheus": (
        lambda: ProviderReaders(prometheus=FakePrometheus()),
        ["resource_pressure", "traffic"],
    ),
    "tempo": (lambda: ProviderReaders(tempo=FakeTempo()), ["runtime_traces"]),
    "all": (
        lambda: ProviderReaders(prometheus=FakePrometheus(), loki=FakeLogs([]), tempo=FakeTempo()),
        ["logs", "resource_pressure", "runtime_traces", "traffic"],
    ),
}


def _live_run(
    world: Any, monkeypatch: pytest.MonkeyPatch, readers: ProviderReaders
) -> tuple[sessionmaker[Session], str, LiveSource]:
    factory, cluster, clock, incident_id = world
    cluster.objects.append(dict(POD))
    seen = Seen(monkeypatch)
    clock.now = T0 + timedelta(minutes=30)
    DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        provider_readers=readers,
        clock=clock,
    ).run(incident_id, "MANUAL")
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
    assert run_id is not None
    return factory, run_id, seen.source


def _payload(factory: sessionmaker[Session], run_id: str) -> dict[str, Any]:
    with factory() as session:
        (row,) = [
            row
            for row in session.scalars(
                select(IncidentEventRow).where(IncidentEventRow.event_type == "EVIDENCE_GATHERED")
            )
            if row.payload.get("run_id") == run_id
        ]
        return dict(row.payload)


def _set_payload(factory: sessionmaker[Session], run_id: str, payload: dict[str, Any]) -> None:
    with factory() as session:
        for row in session.scalars(
            select(IncidentEventRow).where(IncidentEventRow.event_type == "EVIDENCE_GATHERED")
        ):
            if row.payload.get("run_id") == run_id:
                row.payload = payload
        session.commit()


@pytest.mark.parametrize("config", sorted(READERS))
def test_replay_capability_set_equals_the_live_run(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    config: str,
) -> None:
    make_readers, expected = READERS[config]
    factory, run_id, live = _live_run(setup, monkeypatch, make_readers())
    assert live.provider_adapter is not None
    assert list(live.provider_adapter.capabilities()) == expected
    assert _payload(factory, run_id)["provider_capabilities"] == expected
    replay = ReplaySource.from_run(run_id, session_factory=factory)
    assert list(replay.provider_capabilities) == expected
    for name in ASKED:
        assert replay.supports(name) == live.supports(name), name
        assert replay.supports_typed_runtime(name) == live.supports_typed_runtime(name), name


def test_capabilities_come_from_the_boundary_never_the_tape(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prometheus and Tempo configured but never queried: nothing on the tape.
    factory, run_id, _ = _live_run(
        setup,
        monkeypatch,
        ProviderReaders(prometheus=FakePrometheus(), tempo=FakeTempo(), loki=FakeLogs([])),
    )
    with factory() as session:
        taped = set(
            session.scalars(
                select(InvestigationReadRow.capability).where(InvestigationReadRow.run_id == run_id)
            )
        )
    assert taped <= {"loki_logs"}
    replay = ReplaySource.from_run(run_id, session_factory=factory)
    assert replay.supports("resource_pressure") and replay.supports("runtime_traces")
    assert replay.supports_typed_runtime("traffic")
    # And a taped capability the boundary does not list is not supported.
    _set_payload(factory, run_id, {**_payload(factory, run_id), "provider_capabilities": []})
    empty = ReplaySource.from_run(run_id, session_factory=factory)
    assert empty.provider_capabilities == ()
    assert not any(empty.supports_typed_runtime(name) for name in PROVIDER_CAPABILITIES)
    assert not empty.supports("traffic") and empty.supports("logs")  # base logs remain


def test_boundary_without_capabilities_is_not_replayable(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, run_id, _ = _live_run(setup, monkeypatch, ProviderReaders(loki=FakeLogs([])))
    payload = _payload(factory, run_id)
    del payload["provider_capabilities"]
    _set_payload(factory, run_id, payload)
    with pytest.raises(ReplayProviderCapabilitiesMissing):
        ReplaySource.from_run(run_id, session_factory=factory)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(["logs", "tempo"], id="unknown"),
        pytest.param(["traffic", "logs"], id="unsorted"),
        pytest.param(["logs", "logs"], id="duplicate"),
        pytest.param("logs", id="not-a-list"),
        pytest.param(["logs", 1], id="not-strings"),
        pytest.param(None, id="null"),
    ],
)
def test_malformed_capabilities_fail(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    value: Any,
) -> None:
    factory, run_id, _ = _live_run(setup, monkeypatch, ProviderReaders(loki=FakeLogs([])))
    _set_payload(factory, run_id, {**_payload(factory, run_id), "provider_capabilities": value})
    with pytest.raises(ReplayDataError) as raised:
        ReplaySource.from_run(run_id, session_factory=factory)
    assert not isinstance(raised.value, ReplayProviderCapabilitiesMissing)


def test_written_capabilities_are_canonical_and_unknown_names_are_rejected() -> None:
    assert canonical_provider_capabilities(("traffic", "logs", "traffic")) == ["logs", "traffic"]
    assert canonical_provider_capabilities(()) == []
    with pytest.raises(ValueError, match="unknown provider capabilities"):
        canonical_provider_capabilities(("logs", "tempo_traces"))
