"""M19-6.8: the incident-free clean-baseline probe reads persisted evidence and writes nothing."""

from __future__ import annotations

import socket
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from apps.control_plane.baseline import BaselineEvaluation, evaluate_baseline
from apps.control_plane.diagnosis import DiagnosisService
from apps.control_plane.main import create_app
from packages.rca import provider_adapter
from packages.rca.model import EliminationConsequence, EvidenceTemporalRole
from packages.rca.resource_mechanism import RULE_ID as A2_RULE
from packages.storage.database import create_session_factory
from packages.storage.models import Base
from packages.storage.repositories import EventRepository, ObjectVersionRepository

NS = "sre-demo"
COLLECTOR = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
REFERENCE = COLLECTOR + timedelta(minutes=10)
# Every table the probe must leave untouched, and the authoritative evidence.
DECISION_TABLES = (
    "incidents",
    "alerts",
    "diagnoses",
    "incident_events",
    "evidence_requirements",
    "investigation_runs",
    "investigation_reads",
    "run_evidence_manifest",
    "snapshot_cycles",
    "snapshot_cycle_objects",
)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'baseline.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _deployment(memory: str, *, image: str = "payment:1") -> dict[str, Any]:
    labels = {"app": "payment-service"}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "payment-service", "namespace": NS, "uid": "d1", "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": "payment-service",
                            "image": image,
                            "resources": {"limits": {"memory": memory}},
                        }
                    ]
                },
            },
        },
    }


def _warning(at: datetime) -> dict[str, Any]:
    stamp = at.isoformat().replace("+00:00", "Z")
    return {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {"name": "order-service-a.1", "namespace": NS, "uid": "e1"},
        "involvedObject": {"kind": "Pod", "name": "order-service-a", "namespace": NS, "uid": "p1"},
        "reason": "BackOff",
        "message": "Back-off restarting failed container",
        "type": "Warning",
        "count": 3,
        "firstTimestamp": stamp,
        "lastTimestamp": stamp,
    }


def _journal(factory: sessionmaker[Session], *versions: tuple[dict[str, Any], datetime]) -> None:
    with factory() as session:
        for body, at in versions:
            ObjectVersionRepository(session).record(body, at)
        session.commit()


def _events(factory: sessionmaker[Session], *bodies: tuple[dict[str, Any], datetime]) -> None:
    with factory() as session:
        for body, at in bodies:
            EventRepository(session).record(body, at)
        session.commit()


def _probe(factory: sessionmaker[Session], **overrides: Any) -> BaselineEvaluation:
    arguments: dict[str, Any] = {
        "namespaces": (NS,),
        "evidence_namespaces": (),
        "collector_started_at": COLLECTOR,
        "baseline_reference_at": REFERENCE,
    }
    arguments.update(overrides)
    return evaluate_baseline(factory, **arguments)


def _limit_change(factory: sessionmaker[Session], *, changed_at: datetime) -> None:
    _journal(
        factory,
        (_deployment("512Mi"), COLLECTOR + timedelta(minutes=1)),
        (_deployment("128Mi"), changed_at),
    )


def test_a_clean_baseline_has_no_candidate(factory: sessionmaker[Session]) -> None:
    _journal(factory, (_deployment("512Mi"), COLLECTOR + timedelta(minutes=1)))
    evaluation = _probe(factory)
    assert evaluation.initiating_finding_count == 0
    assert evaluation.root_eligible_manifestation_only_count == 0
    document = evaluation.document()
    assert document["reference_at"] == REFERENCE.isoformat()
    assert document["window_start"] == COLLECTOR.isoformat()
    assert "symptoms" not in document and "revision_number" not in document


def test_a_persisted_spec_change_is_an_initiating_finding(factory: sessionmaker[Session]) -> None:
    _limit_change(factory, changed_at=COLLECTOR + timedelta(minutes=3))
    evaluation = _probe(factory)
    assert evaluation.initiating_finding_count == 1
    (finding,) = [item for item in evaluation.findings if item.kind.value == "SPEC_CHANGE"]
    assert finding.temporal_role is EvidenceTemporalRole.INITIATING
    # Its hypothesis carries the initiating premise, so it is not manifestation-only.
    assert all(item.initiating_findings for item in evaluation.hypotheses)
    assert evaluation.root_eligible_manifestation_only_count == 0


def test_a_warning_manifestation_is_a_root_eligible_manifestation_only_candidate(
    factory: sessionmaker[Session],
) -> None:
    _events(factory, (_warning(COLLECTOR + timedelta(minutes=4)), COLLECTOR + timedelta(minutes=4)))
    evaluation = _probe(factory)
    assert evaluation.initiating_finding_count == 0
    assert evaluation.root_eligible_manifestation_only_count == 1
    (hypothesis_id,) = evaluation.root_eligible_manifestation_only_hypothesis_ids
    hypothesis = next(item for item in evaluation.hypotheses if item.hypothesis_id == hypothesis_id)
    assert hypothesis.initiating_findings == ()
    assert not any(
        item.hypothesis_id == hypothesis_id
        and item.consequence is EliminationConsequence.ROOT_INELIGIBILITY
        for item in evaluation.eliminations
    )


def _pod() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "order-service-a", "namespace": NS, "uid": "p1"},
        "spec": {"containers": [{"name": "order-service", "image": "order:1"}]},
    }


def test_a_root_ineligible_manifestation_is_not_counted(factory: sessionmaker[Session]) -> None:
    """The Pod's episode ended (exact-UID tombstone) before the reference: A1 TERMINATED."""
    _journal(factory, (_pod(), COLLECTOR + timedelta(minutes=1)))
    _events(factory, (_warning(COLLECTOR + timedelta(minutes=2)), COLLECTOR + timedelta(minutes=2)))
    with factory() as session:
        assert ObjectVersionRepository(session).tombstone(
            f"{NS}/Pod/order-service-a", COLLECTOR + timedelta(minutes=5)
        )
        session.commit()
    evaluation = _probe(factory)
    manifestation_only = [item for item in evaluation.hypotheses if not item.initiating_findings]
    assert manifestation_only  # the candidate exists ...
    ineligible = {
        item.hypothesis_id
        for item in evaluation.eliminations
        if item.consequence is EliminationConsequence.ROOT_INELIGIBILITY
    }
    assert {item.hypothesis_id for item in manifestation_only} <= ineligible
    assert evaluation.root_eligible_manifestation_only_count == 0  # ... but is not root-eligible


def test_only_the_collector_to_reference_window_is_read(factory: sessionmaker[Session]) -> None:
    # A change before the collector started and one after the reference time.
    _journal(
        factory,
        (_deployment("512Mi"), COLLECTOR - timedelta(minutes=5)),
        (_deployment("256Mi"), COLLECTOR - timedelta(minutes=2)),
        (_deployment("128Mi"), REFERENCE + timedelta(minutes=1)),
    )
    _events(factory, (_warning(REFERENCE + timedelta(minutes=2)), REFERENCE + timedelta(minutes=2)))
    evaluation = _probe(factory)
    assert (
        evaluation.initiating_finding_count,
        evaluation.root_eligible_manifestation_only_count,
    ) == (
        0,
        0,
    )


def test_provider_state_is_not_assessed(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def bomb(*_: Any, **__: Any) -> Any:
        raise AssertionError("the baseline probe reached a provider")

    monkeypatch.setattr(provider_adapter.ProviderAdapter, "__init__", bomb)
    monkeypatch.setattr(provider_adapter.ProviderReaders, "from_environment", bomb)
    monkeypatch.setattr(urllib.request, "urlopen", bomb)
    monkeypatch.setattr(socket.socket, "connect", bomb)
    _limit_change(factory, changed_at=COLLECTOR + timedelta(minutes=3))
    evaluation = _probe(factory)
    # A lowered memory limit with no provider evidence: nothing about resource
    # pressure is concluded — no A2 elimination, no contradiction.
    assert not any(item.rule_id == A2_RULE for item in evaluation.eliminations)
    assert not any(
        item.consequence is EliminationConsequence.CONTRADICTION for item in evaluation.eliminations
    )
    assert evaluation.initiating_finding_count == 1  # still a dirty baseline


def _counts(factory: sessionmaker[Session]) -> dict[str, int]:
    with factory() as session:
        return {
            table.name: session.scalar(select(func.count()).select_from(table)) or 0
            for table in Base.metadata.sorted_tables
        }


def test_the_probe_writes_nothing(factory: sessionmaker[Session]) -> None:
    _limit_change(factory, changed_at=COLLECTOR + timedelta(minutes=3))
    _events(factory, (_warning(COLLECTOR + timedelta(minutes=4)), COLLECTOR + timedelta(minutes=4)))
    before = _counts(factory)
    assert set(DECISION_TABLES) <= set(before)
    _probe(factory)
    _probe(factory)
    assert _counts(factory) == before
    assert all(before[name] == 0 for name in DECISION_TABLES)


@pytest.mark.parametrize(
    "overrides",
    [
        {"collector_started_at": REFERENCE + timedelta(seconds=1)},
        {"baseline_reference_at": REFERENCE.replace(tzinfo=None)},
    ],
)
def test_inconsistent_windows_are_refused(factory: sessionmaker[Session], overrides: Any) -> None:
    with pytest.raises(ValueError):
        _probe(factory, **overrides)


def _client(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SRE_API_TOKEN", "t0ken")
    service = DiagnosisService(session_factory=factory, namespaces=(NS,), evidence_namespaces=())
    return TestClient(create_app(session_factory=factory, diagnosis_service=service))


def _body(**overrides: Any) -> dict[str, Any]:
    return {
        "namespace": NS,
        "baseline_reference_at": REFERENCE.isoformat(),
        "collector_started_at": COLLECTOR.isoformat(),
        **overrides,
    }


def test_the_endpoint_is_protected_and_returns_the_ephemeral_evaluation(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _limit_change(factory, changed_at=COLLECTOR + timedelta(minutes=3))
    client = _client(factory, monkeypatch)
    assert client.post("/api/v1/baseline-probe", json=_body()).status_code == 401
    before = _counts(factory)
    response = client.post(
        "/api/v1/baseline-probe", json=_body(), headers={"Authorization": "Bearer t0ken"}
    )
    assert response.status_code == 200
    document = response.json()
    assert document["initiating_finding_count"] == 1
    assert document["root_eligible_manifestation_only_count"] == 0
    assert _counts(factory) == before
    wrong = client.post(
        "/api/v1/baseline-probe",
        json=_body(namespace="default"),
        headers={"Authorization": "Bearer t0ken"},
    )
    assert wrong.status_code == 422
