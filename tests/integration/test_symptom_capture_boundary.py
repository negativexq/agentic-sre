"""m21 §36.2: the symptom captures are frozen at the run boundary, and replay reads them as frozen."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from test_evidence_manifest import POD, Seen
from test_live_diagnosis import T0, FakeLogs, setup  # noqa: F401 - pytest fixture

from apps.control_plane.diagnosis import DiagnosisService
from packages.contracts import IncidentEventType
from packages.rca.provider_adapter import ProviderReaders
from packages.rca.replay import ReplaySource
from packages.storage.manifest import load_run_boundary
from packages.storage.models import IncidentEventRow, IncidentRow
from packages.storage.repositories import DiagnosisRepository

FIRED = T0 + timedelta(minutes=11)
EXPRESSION = 'sum(rate(http_requests_total{service="order-service",status=~"5.."}[30s])) > 0.05'


class Rules:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def alert_rule(self, alert: str, labels: Mapping[str, str], at: datetime) -> dict[str, object]:
        self.calls.append(alert)
        return {
            "expression": EXPRESSION,
            "for_seconds": 15.0,
            "interval_seconds": 5.0,
            "scrape_interval_seconds": 5.0,
        }

    def query_traffic(self, *args: Any) -> tuple[()]:
        return ()

    def query_resource_pressure(self, *args: Any) -> tuple[()]:
        return ()


class ExecutionLogs(FakeLogs):
    def execution_records(
        self, namespace: str, service: str, starts_at: datetime, ends_at: datetime
    ) -> list[dict[str, str]]:
        at_ns = int((FIRED - timedelta(seconds=10)).timestamp() * 1e9)
        return [
            {
                "execution_record": "operation",
                "trace_id": "t1",
                "execution_operation_kind": "HTTP_CLIENT",
                "execution_operation_outcome": "ERROR",
                "execution_operation_started_ns": str(at_ns),
                "execution_operation_duration_ns": "1000",
                "server_address": "payment-service",
                "error_type": "ConnectionRefusedError",
            },
            {
                "execution_record": "terminal",
                "trace_id": "t1",
                "http_response_status_code": "503",
                "timestamp_ns": str(at_ns + 2000),
            },
        ]


def _run(
    world: Any, monkeypatch: pytest.MonkeyPatch, readers: ProviderReaders
) -> tuple[Any, str, Seen]:
    factory, cluster, clock, incident_id = world
    cluster.objects.append(dict(POD))
    service = DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        provider_readers=readers,
        clock=clock,
    )
    seen = Seen(monkeypatch)
    clock.now = T0 + timedelta(minutes=30)
    service.run(incident_id, "MANUAL")
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
    assert run_id is not None
    return factory, run_id, seen


def test_the_captures_are_frozen_at_the_boundary_and_replayed_as_frozen(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = Rules()
    factory, run_id, seen = _run(
        setup,
        monkeypatch,
        ProviderReaders(prometheus=rules, loki=ExecutionLogs([])),
    )
    live = tuple(seen.source.symptom_captures())
    (capture,) = live
    assert rules.calls == ["HighLatency"]
    assert capture.rule.expression == EXPRESSION and capture.rule.fired_at == FIRED
    assert capture.execution is not None
    assert [t.status for t in capture.execution.terminals] == [503]
    assert capture.execution.starts_at == FIRED - timedelta(seconds=45)
    with factory() as session:
        assert load_run_boundary(session, run_id).symptom_captures == live
    assert tuple(ReplaySource.from_run(run_id, session_factory=factory).symptom_captures()) == live


def test_a_run_without_the_reads_freezes_no_captures(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, run_id, seen = _run(setup, monkeypatch, ProviderReaders(loki=FakeLogs([])))
    assert tuple(seen.source.symptom_captures()) == ()
    with factory() as session:
        payload = session.scalars(
            select(IncidentEventRow.payload).where(
                IncidentEventRow.event_type == IncidentEventType.EVIDENCE_GATHERED.value
            )
        ).one()
        # the boundary of a run without captures is as it was before them
        assert "symptom_captures" not in payload
        assert load_run_boundary(session, run_id).symptom_captures == ()
    assert tuple(ReplaySource.from_run(run_id, session_factory=factory).symptom_captures()) == ()


class CountingRules(Rules):
    """Each read's group interval is its ordinal, so a capture names the read it came from."""

    def alert_rule(self, alert: str, labels: Mapping[str, str], at: datetime) -> dict[str, object]:
        return {**super().alert_rule(alert, labels, at), "interval_seconds": float(len(self.calls))}


def test_a_resolved_incident_freezes_the_captures_its_runs_took_before_the_resolution(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # m21 §36.8: the resolved run reads nothing; it takes the latest capture frozen by the resolution
    factory, cluster, clock, incident_id = setup
    cluster.objects.append(dict(POD))
    rules = CountingRules()
    service = DiagnosisService(
        session_factory=factory,
        namespaces=("sre-demo",),
        reader=cluster,
        provider_readers=ProviderReaders(prometheus=rules, loki=ExecutionLogs([])),
        clock=clock,
    )
    for minute in (13, 14, 16):
        clock.now = T0 + timedelta(minutes=minute)
        service.run(incident_id, "MANUAL")
    with factory() as session:
        row = session.get(IncidentRow, incident_id)
        assert row is not None
        row.status = "CLOSED"
        row.updated_at = T0 + timedelta(minutes=15)
        session.commit()
    seen = Seen(monkeypatch)
    clock.now = T0 + timedelta(minutes=30)
    service.run(incident_id, "MANUAL")
    assert len(rules.calls) == 3
    (capture,) = seen.source.symptom_captures()
    # the run at minute 14 took the second read; the one at minute 16 is after the resolution
    assert capture.rule.interval_seconds == 2.0
    with factory() as session:
        run_id = DiagnosisRepository(session).latest_run_id(incident_id)
        assert run_id is not None
        assert load_run_boundary(session, run_id).symptom_captures == (capture,)
    assert tuple(ReplaySource.from_run(run_id, session_factory=factory).symptom_captures()) == (
        capture,
    )
