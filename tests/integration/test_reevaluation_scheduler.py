"""M19-5.6: OPEN evidence requirements trigger deadline revisions, and nothing more."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker
from test_requirement_lifecycle import (
    AT,
    NOT_BEFORE,
    _a1,
    _a2,
    _factory,
    _incident,
)

from apps.control_plane.diagnosis import DiagnosisService
from apps.control_plane.scheduler import (
    EVIDENCE_DEADLINE,
    ReevaluationConfig,
    SchedulerPass,
    reevaluation_from_environment,
    run_scheduler_pass,
)
from packages.rca.model import (
    Confidence,
    Diagnosis,
    HypothesisInventoryEntry,
    PreconditionResult,
    RequirementAuditReason,
    RequirementEvaluation,
    Symptoms,
)
from packages.storage.models import DiagnosisRow, EvidenceRequirementRow
from packages.storage.repositories import (
    DiagnosisRepository,
    EvidenceRequirementRepository,
    RequirementOnsetUnavailable,
)

ONSET = AT  # the onset every fixture revision records
CONFIG = ReevaluationConfig()  # settle 90 s, max 6 revisions, horizon 2 h
DUE = NOT_BEFORE + CONFIG.settle
HORIZON = ONSET + CONFIG.horizon


@pytest.fixture
def factory(tmp_path: Any) -> Iterator[sessionmaker[Session]]:
    made = _factory(f"sqlite:///{tmp_path / 'scheduler.db'}")
    yield made
    made.kw["bind"].dispose()


def _document(
    evaluations: Sequence[RequirementEvaluation],
    *,
    onset: datetime | None = ONSET,
    keys: Sequence[str | None] | None = None,
) -> Diagnosis:
    inventory = (
        list(keys)
        if keys is not None
        else list(dict.fromkeys(item.hypothesis_key for item in evaluations))
    )
    return Diagnosis(
        incident_id="synthetic",
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        summary="synthetic",
        symptoms=Symptoms(onset=onset, last_seen=onset, services=(), namespaces=(), alert_names=()),
        requirement_evaluations=tuple(evaluations),
        hypothesis_inventory=tuple(
            HypothesisInventoryEntry(hypothesis_id=f"hypothesis:{index}", hypothesis_key=key)
            for index, key in enumerate(inventory)
        ),
    )


def _write(
    factory: sessionmaker[Session],
    incident_id: UUID,
    *evaluations: RequirementEvaluation,
    trigger: str = "MANUAL",
    window_end: datetime = ONSET,
    onset: datetime | None = ONSET,
) -> int:
    """One revision written as the service writes it: lifecycle in the same transaction."""
    diagnosis = _document(evaluations, onset=onset)

    def companions(session: Session, diagnosis_id: int) -> None:
        EvidenceRequirementRepository(session).apply_revision(
            incident_id=incident_id, diagnosis_id=diagnosis_id, diagnosis=diagnosis
        )

    with factory() as session:
        return (
            DiagnosisRepository(session)
            .save_revision(
                incident_id=incident_id,
                document=diagnosis.model_dump(mode="json"),
                created_at=window_end,
                run_id=None,
                trigger=trigger,
                window_end=window_end,
                manifest_digest="m" * 64,
                tape_digest="t" * 64,
                epistemic_digest="e" * 64,
                engine_version="test",
                config_digest="c" * 64,
                companions=companions,
            )
            .diagnosis_id
        )


class FakeService:
    """Stands in for ``DiagnosisService.run``: records the call, writes the revision.

    The written deadline revision evaluates every requirement with ``outcome``
    at ``window_end = now``, exactly what a real deadline revision persists.
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        outcome: dict[UUID, Sequence[RequirementEvaluation]] | None = None,
    ) -> None:
        self.factory = factory
        self.outcome = outcome or {}
        self.calls: list[tuple[UUID, str]] = []
        self.now = ONSET

    def run(self, incident_id: UUID, trigger: str) -> None:
        self.calls.append((incident_id, trigger))
        _write(
            self.factory,
            incident_id,
            *self.outcome.get(incident_id, ()),
            trigger=trigger,
            window_end=self.now,
        )


def _pass(
    factory: sessionmaker[Session], service: FakeService, now: datetime, **config: Any
) -> SchedulerPass:
    service.now = now
    return run_scheduler_pass(
        factory, ReevaluationConfig(**config) if config else CONFIG, now=now, run=service.run
    )


def _statuses(factory: sessionmaker[Session], incident_id: UUID) -> list[str]:
    with factory() as session:
        return [
            row.status for row in EvidenceRequirementRepository(session).for_incident(incident_id)
        ]


def _triggers(factory: sessionmaker[Session], incident_id: UUID) -> list[str]:
    with factory() as session:
        return [row.trigger for row in DiagnosisRepository(session).list_revisions(incident_id)]


NO_DATA = _a2(audit_reason=RequirementAuditReason.NO_DATA_AFTER_DEADLINE)
PARTIAL = _a2(audit_reason=RequirementAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE)


# --- configuration ----------------------------------------------------------


def test_reevaluation_is_opt_in_and_bounds_are_parsed_strictly() -> None:
    assert reevaluation_from_environment({}) is None
    assert reevaluation_from_environment({"SRE_REEVALUATE": "false"}) is None
    assert reevaluation_from_environment({"SRE_REEVALUATE": "true"}) == ReevaluationConfig(
        settle=timedelta(seconds=90), max_revisions=6, horizon=timedelta(hours=2)
    )
    assert reevaluation_from_environment(
        {
            "SRE_REEVALUATE": "TRUE",
            "SRE_REEVALUATE_SETTLE_SECONDS": "30",
            "SRE_MAX_REVISIONS": "4",
            "SRE_REEVALUATE_HORIZON": "90m",
        }
    ) == ReevaluationConfig(
        settle=timedelta(seconds=30), max_revisions=4, horizon=timedelta(minutes=90)
    )
    for bad in (
        {"SRE_REEVALUATE_HORIZON": "2 hours"},
        {"SRE_REEVALUATE_SETTLE_SECONDS": "-5"},
        {"SRE_MAX_REVISIONS": "0"},
    ):
        with pytest.raises(ValueError):
            reevaluation_from_environment({"SRE_REEVALUATE": "true", **bad})


def test_disabled_reevaluation_never_schedules(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2())
    service = DiagnosisService(session_factory=factory, namespaces=(), clock=lambda: DUE)
    calls: list[tuple[UUID, str]] = []
    monkeypatch.setattr(service, "run", lambda *args: calls.append(args))
    assert service.reevaluation is None
    assert service.reevaluate() is None
    assert calls == []
    assert _statuses(factory, incident) == ["OPEN"]


def test_the_service_pass_triggers_exactly_evidence_deadline(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2())
    service = DiagnosisService(
        session_factory=factory, namespaces=(), clock=lambda: DUE, reevaluation=CONFIG
    )
    calls: list[tuple[UUID, str]] = []
    monkeypatch.setattr(service, "run", lambda *args: calls.append(args))
    result = service.reevaluate()
    assert calls == [(incident, "EVIDENCE_DEADLINE")] and EVIDENCE_DEADLINE == "EVIDENCE_DEADLINE"
    assert result is not None and result.triggered == (incident,)


# --- due boundary and grouping ------------------------------------------------


def test_settle_boundary_is_inclusive(factory: sessionmaker[Session]) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2())
    service = FakeService(factory)
    assert _pass(factory, service, DUE - timedelta(seconds=1)).triggered == ()
    assert _pass(factory, service, NOT_BEFORE).triggered == ()  # due only after settling
    assert _pass(factory, service, DUE).triggered == (incident,)
    assert service.calls == [(incident, "EVIDENCE_DEADLINE")]


def test_due_requirements_of_one_incident_share_one_revision(
    factory: sessionmaker[Session],
) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a1("uid-a"), _a1("uid-b"), _a2())
    assert _statuses(factory, incident) == ["OPEN"] * 3
    service = FakeService(factory)
    assert _pass(factory, service, DUE).triggered == (incident,)
    assert service.calls == [(incident, "EVIDENCE_DEADLINE")]


def test_each_due_incident_gets_one_revision(factory: sessionmaker[Session]) -> None:
    first, second = _incident(factory), _incident(factory)
    _write(factory, first, _a2(), _a1("uid-a"))
    _write(factory, second, _a2())
    service = FakeService(factory)
    result = _pass(factory, service, DUE)
    assert sorted(result.triggered) == sorted([first, second])
    assert sorted(service.calls) == sorted(
        [(first, EVIDENCE_DEADLINE), (second, EVIDENCE_DEADLINE)]
    )


# --- the consumed-deadline guard ----------------------------------------------


def test_manual_and_initial_revisions_never_consume_the_deadline(
    factory: sessionmaker[Session],
) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2(), trigger="INITIAL")
    # A MANUAL revision after not_before that leaves the requirement OPEN.
    _write(
        factory, incident, NO_DATA, trigger="MANUAL", window_end=NOT_BEFORE + timedelta(minutes=5)
    )
    assert _statuses(factory, incident) == ["OPEN"]
    service = FakeService(factory)
    assert _pass(factory, service, DUE + timedelta(minutes=10)).triggered == (incident,)


@pytest.mark.parametrize(
    ("offset", "consumed"),
    [
        pytest.param(timedelta(seconds=-1), False, id="window-before-not-before"),
        pytest.param(timedelta(0), True, id="window-at-not-before"),
        pytest.param(timedelta(minutes=3), True, id="window-after-not-before"),
    ],
)
def test_a_later_deadline_revision_consumes_only_when_its_window_reaches_not_before(
    factory: sessionmaker[Session], offset: timedelta, consumed: bool
) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2())
    _write(factory, incident, NO_DATA, trigger=EVIDENCE_DEADLINE, window_end=NOT_BEFORE + offset)
    assert _statuses(factory, incident) == ["OPEN"]
    service = FakeService(factory)
    triggered = _pass(factory, service, DUE + timedelta(minutes=10)).triggered
    assert triggered == (() if consumed else (incident,))


def test_an_older_deadline_revision_cannot_consume_a_newly_opened_requirement(
    factory: sessionmaker[Session],
) -> None:
    incident = _incident(factory)
    _write(factory, incident, trigger=EVIDENCE_DEADLINE, window_end=NOT_BEFORE + timedelta(hours=1))
    _write(factory, incident, _a2())  # opened after that deadline revision
    service = FakeService(factory)
    assert _pass(factory, service, DUE).triggered == (incident,)


def _no_data_chain(factory: sessionmaker[Session]) -> None:
    """The critical chain: R1 opens K → R2 deadline NO_DATA → no R3 → EXPIRED."""
    incident = _incident(factory)
    _write(factory, incident, _a2(), trigger="INITIAL")  # R1 opens K
    service = FakeService(factory, {incident: [NO_DATA]})
    assert _pass(factory, service, DUE).triggered == (incident,)  # R2
    assert _triggers(factory, incident) == ["INITIAL", EVIDENCE_DEADLINE]
    assert _statuses(factory, incident) == ["OPEN"]  # NO_DATA leaves K OPEN
    for later in (DUE, DUE + timedelta(minutes=5), HORIZON - timedelta(seconds=1)):
        result = _pass(factory, service, later)
        assert result.triggered == () and result.expired == ()
    assert len(service.calls) == 1  # no R3 for the consumed deadline
    with factory() as session:
        (row,) = EvidenceRequirementRepository(session).for_incident(incident)
    result = _pass(factory, service, HORIZON)
    assert result.expired == (row.requirement_id,) and result.triggered == ()
    assert _statuses(factory, incident) == ["EXPIRED"]
    assert _pass(factory, service, HORIZON + timedelta(hours=1)) == SchedulerPass()
    assert _triggers(factory, incident) == ["INITIAL", EVIDENCE_DEADLINE]


def test_no_data_deadline_revision_is_not_retriggered_and_expires_at_the_horizon(
    factory: sessionmaker[Session],
) -> None:
    _no_data_chain(factory)


@pytest.mark.postgres
def test_no_data_chain_on_postgres(postgres_url: str) -> None:
    made = _factory(postgres_url)
    try:
        _no_data_chain(made)
    finally:
        made.kw["bind"].dispose()


def test_partial_deadline_revision_is_not_retriggered(factory: sessionmaker[Session]) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2())
    service = FakeService(factory, {incident: [PARTIAL]})
    assert _pass(factory, service, DUE).triggered == (incident,)
    assert _pass(factory, service, DUE).triggered == ()  # same instant, repeat poll
    assert _pass(factory, service, DUE + timedelta(minutes=30)).triggered == ()
    assert service.calls == [(incident, EVIDENCE_DEADLINE)]


def test_a_satisfying_deadline_revision_closes_the_requirement(
    factory: sessionmaker[Session],
) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2())
    service = FakeService(factory, {incident: [_a2(result=PreconditionResult.passed())]})
    assert _pass(factory, service, DUE).triggered == (incident,)
    assert _statuses(factory, incident) == ["SATISFIED_BY_REVISION"]
    assert _pass(factory, service, DUE + timedelta(minutes=5)) == SchedulerPass()


# --- bounds ----------------------------------------------------------------


@pytest.mark.parametrize(("existing", "allowed"), [(5, True), (6, False)])
def test_max_revisions_is_an_inclusive_bound(
    factory: sessionmaker[Session], existing: int, allowed: bool
) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2())
    for _ in range(existing - 1):
        _write(factory, incident, NO_DATA, window_end=ONSET)  # MANUAL, K stays OPEN
    with factory() as session:
        assert DiagnosisRepository(session).revision_count(incident) == existing
    service = FakeService(factory, {incident: [NO_DATA]})
    result = _pass(factory, service, DUE)
    assert result.triggered == ((incident,) if allowed else ())
    assert result.at_max_revisions == (() if allowed else (incident,))
    assert _statuses(factory, incident) == ["OPEN"]  # no fake diagnosis, no RCA change


def test_horizon_is_inclusive_and_anchored_to_the_opening_onset(
    factory: sessionmaker[Session],
) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2())
    service = FakeService(factory, {incident: [NO_DATA]})
    before = _pass(factory, service, HORIZON - timedelta(seconds=1))
    assert before.expired == () and before.triggered == (incident,)
    at = _pass(factory, service, HORIZON)  # onset + 2h, not not_before + 2h
    assert len(at.expired) == 1 and at.triggered == ()
    assert _statuses(factory, incident) == ["EXPIRED"]


def test_an_expired_requirement_is_never_triggered_even_when_due(
    factory: sessionmaker[Session],
) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2())
    service = FakeService(factory)
    result = _pass(factory, service, HORIZON + timedelta(minutes=1))
    assert len(result.expired) == 1 and result.triggered == () and service.calls == []


def test_closed_requirements_never_schedule(factory: sessionmaker[Session]) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a1("uid-a"), _a1("uid-b"), _a1("uid-c"))
    with factory() as session:
        rows = EvidenceRequirementRepository(session).for_incident(incident)
        for row, status in zip(
            rows, ("SATISFIED_BY_REVISION", "SUPERSEDED_BY_REVISION", "EXPIRED"), strict=True
        ):
            session.execute(
                update(EvidenceRequirementRow)
                .where(EvidenceRequirementRow.requirement_id == row.requirement_id)
                .values(status=status)
            )
        session.commit()
    service = FakeService(factory)
    assert _pass(factory, service, DUE) == SchedulerPass()
    assert _pass(factory, service, HORIZON) == SchedulerPass()


def test_only_the_replacement_open_row_is_considered(factory: sessionmaker[Session]) -> None:
    incident = _incident(factory)
    _write(factory, incident, _a2(), trigger="INITIAL")  # R1 opens K
    earlier = ONSET - timedelta(minutes=30)
    # R_early (MANUAL, still PENDING) supersedes K and reopens it; its own
    # revision records an earlier onset (an earlier alert arrived).
    r_early = _write(factory, incident, _a2(), trigger="MANUAL", onset=earlier)
    with factory() as session:
        rows = EvidenceRequirementRepository(session).for_incident(incident)
    assert [(row.status, row.diagnosis_id == r_early) for row in rows] == [
        ("SUPERSEDED_BY_REVISION", False),
        ("OPEN", True),
    ]
    service = FakeService(factory, {incident: [NO_DATA]})
    assert _pass(factory, service, DUE).triggered == (incident,)
    # The replacement's horizon follows its own opening revision's onset.
    result = _pass(factory, service, earlier + CONFIG.horizon)
    assert result.expired == (rows[1].requirement_id,)
    assert _statuses(factory, incident) == ["SUPERSEDED_BY_REVISION", "EXPIRED"]


# --- persisted-state corruption -------------------------------------------------


def test_a_missing_opening_onset_fails_the_pass_without_expiring_or_triggering(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy, broken = _incident(factory), _incident(factory)
    _write(factory, healthy, _a2())
    _write(factory, broken, _a2(), onset=None)
    service = FakeService(factory)
    with pytest.raises(RequirementOnsetUnavailable, match="no onset"):
        _pass(factory, service, HORIZON + timedelta(hours=1))
    assert _statuses(factory, healthy) == ["OPEN"]  # nothing expired by the failed pass
    assert _statuses(factory, broken) == ["OPEN"]
    assert service.calls == []
    # An unreadable opening document fails the same way, never substituting a time.
    with factory() as session:
        (row,) = EvidenceRequirementRepository(session).for_incident(broken)
        session.execute(
            update(DiagnosisRow)
            .where(DiagnosisRow.diagnosis_id == row.diagnosis_id)
            .values(document={"summary": "no symptoms"})
        )
        session.commit()
    with pytest.raises(RequirementOnsetUnavailable, match="no readable symptoms"):
        _pass(factory, service, DUE)
    # Through the watch-loop entry point the failure is logged, not raised.
    live = DiagnosisService(
        session_factory=factory, namespaces=(), clock=lambda: DUE, reevaluation=CONFIG
    )
    monkeypatch.setattr(live, "run", service.run)
    assert live.reevaluate() is None
    assert service.calls == []
