"""M19-5.5: requirement lifecycle across diagnosis revisions, in the revision's transaction."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_live_diagnosis import T0, setup  # noqa: F401 - pytest fixture

import apps.control_plane.diagnosis as diagnosis_module
from apps.control_plane.diagnosis import DiagnosisService
from packages.contracts import Incident, IncidentSeverity, IncidentSource, IncidentStatus
from packages.rca.engine import diagnose as engine_diagnose
from packages.rca.model import (
    Confidence,
    Diagnosis,
    HypothesisInventoryEntry,
    PreconditionResult,
    RequirementAuditReason,
    RequirementEvaluation,
    RequirementKind,
    RequirementTarget,
    Symptoms,
)
from packages.rca.requirements import requirement_key
from packages.storage.database import create_session_factory
from packages.storage.models import Base, DiagnosisRow, EvidenceRequirementRow
from packages.storage.repositories import (
    DiagnosisRepository,
    EvidenceRequirementRepository,
    IncidentRepository,
    RequirementTransitions,
)

AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
NOT_BEFORE = AT + timedelta(minutes=15)
A1 = ("m16.ended-manifestation-episode", "v1")
A2 = ("m16.resource-pressure", "v1")


def _factory(url: str) -> sessionmaker[Session]:
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def sqlite_factory(tmp_path: Any) -> Iterator[sessionmaker[Session]]:
    factory = _factory(f"sqlite:///{tmp_path / 'requirements.db'}")
    yield factory
    factory.kw["bind"].dispose()


def _incident(factory: sessionmaker[Session]) -> UUID:
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="latency",
        created_at=AT,
        updated_at=AT,
    )
    with factory() as session:
        IncidentRepository(session).create(incident)
        session.commit()
    return incident.incident_id


def _outcome(result: Any, audit_reason: Any) -> dict[str, Any]:
    if audit_reason is not None:
        return {"result": None, "audit_reason": audit_reason}
    return {"result": result or PreconditionResult.pending(NOT_BEFORE)}


def _a1(
    uid: str,
    result: Any = None,
    key: str = "hkey:worker",
    *,
    hypothesis_key: Any = "",
    audit_reason: Any = None,
) -> RequirementEvaluation:
    return RequirementEvaluation(
        hypothesis_id=f"hypothesis:{key}",
        hypothesis_key=key if hypothesis_key == "" else hypothesis_key,
        rule_id=A1[0],
        rule_version=A1[1],
        kind=RequirementKind.STATUS_CONTINUITY,
        targets=(RequirementTarget(entity="shop/Pod/worker-0", uid=uid),),
        **_outcome(result, audit_reason),
    )


def _a2(
    series: tuple[tuple[str, str], ...] = (("app", "memory"),),
    result: Any = None,
    *,
    audit_reason: Any = None,
) -> RequirementEvaluation:
    return RequirementEvaluation(
        hypothesis_id="hypothesis:payment",
        hypothesis_key="hkey:payment",
        rule_id=A2[0],
        rule_version=A2[1],
        kind=RequirementKind.RESOURCE_COVERAGE,
        targets=tuple(
            RequirementTarget(entity="shop/Deployment/payment", container=c, resource=r)
            for c, r in series
        ),
        **_outcome(result, audit_reason),
    )


def _diagnosis(
    *evaluations: RequirementEvaluation, keys: tuple[str | None, ...] | None = None
) -> Diagnosis:
    inventory_keys = (
        keys
        if keys is not None
        else tuple(dict.fromkeys(item.hypothesis_key for item in evaluations))
    )
    return Diagnosis(
        incident_id="synthetic",
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        summary="synthetic",
        symptoms=Symptoms(onset=AT, last_seen=AT, services=(), namespaces=(), alert_names=()),
        requirement_evaluations=evaluations,
        hypothesis_inventory=tuple(
            HypothesisInventoryEntry(hypothesis_id=f"hypothesis:{index}", hypothesis_key=key)
            for index, key in enumerate(inventory_keys)
        ),
    )


def _revise(
    factory: sessionmaker[Session],
    incident_id: UUID,
    diagnosis: Diagnosis,
    *,
    after: Callable[[Session], None] | None = None,
) -> tuple[int, RequirementTransitions]:
    """Write one revision exactly as the service does: lifecycle as a companion."""
    seen: list[RequirementTransitions] = []

    def companions(session: Session, diagnosis_id: int) -> None:
        seen.append(
            EvidenceRequirementRepository(session).apply_revision(
                incident_id=incident_id, diagnosis_id=diagnosis_id, diagnosis=diagnosis
            )
        )
        if after is not None:
            after(session)

    with factory() as session:
        revision = DiagnosisRepository(session).save_revision(
            incident_id=incident_id,
            document=diagnosis.model_dump(mode="json"),
            created_at=AT,
            run_id=None,
            trigger="MANUAL",
            window_end=AT,
            manifest_digest="m" * 64,
            tape_digest="t" * 64,
            epistemic_digest="e" * 64,
            engine_version="test",
            config_digest="c" * 64,
            companions=companions,
        )
    (transitions,) = seen
    return revision.diagnosis_id, transitions


def _rows(factory: sessionmaker[Session], incident_id: UUID) -> list[tuple[str, str, int, str]]:
    with factory() as session:
        return [
            (row.requirement_key, row.status, row.diagnosis_id, row.kind)
            for row in EvidenceRequirementRepository(session).for_incident(incident_id)
        ]


def _open(factory: sessionmaker[Session], incident_id: UUID) -> list[str]:
    return [key for key, status, *_ in _rows(factory, incident_id) if status == "OPEN"]


def _key(incident_id: UUID, evaluation: RequirementEvaluation) -> str:
    return requirement_key(incident_id, evaluation)


# --- the plan's done criterion ------------------------------------------------


def _pending_then_pass(factory: sessionmaker[Session]) -> None:
    incident = _incident(factory)
    key = _key(incident, _a2())
    r1, first = _revise(factory, incident, _diagnosis(_a2()))
    assert first == RequirementTransitions(opened=(key,))
    r_early, second = _revise(factory, incident, _diagnosis(_a2()))
    assert second == RequirementTransitions(opened=(key,), superseded=(key,))
    assert _open(factory, incident) == [key]  # exactly one OPEN
    r2, third = _revise(factory, incident, _diagnosis(_a2(result=PreconditionResult.passed())))
    assert third == RequirementTransitions(satisfied=(key,))
    assert _rows(factory, incident) == [
        (key, "SUPERSEDED_BY_REVISION", r1, "RESOURCE_COVERAGE"),
        (key, "SATISFIED_BY_REVISION", r_early, "RESOURCE_COVERAGE"),
    ]
    assert r1 < r_early < r2
    with factory() as session:
        row = EvidenceRequirementRepository(session).for_incident(incident)[-1]
        assert row.targets == [
            {"entity": "shop/Deployment/payment", "container": "app", "resource": "memory"}
        ]
        assert row.not_before == NOT_BEFORE
        assert (row.rule_id, row.rule_version, row.hypothesis_key) == (*A2, "hkey:payment")


def test_pending_supersedes_to_one_open_and_pass_satisfies(
    sqlite_factory: sessionmaker[Session],
) -> None:
    _pending_then_pass(sqlite_factory)


@pytest.mark.postgres
def test_pending_supersedes_to_one_open_and_pass_satisfies_on_postgres(postgres_url: str) -> None:
    factory = _factory(postgres_url)
    try:
        _pending_then_pass(factory)
    finally:
        factory.kw["bind"].dispose()


# --- the frozen matrix --------------------------------------------------------


def test_disqualified_satisfies_the_requirement(sqlite_factory: sessionmaker[Session]) -> None:
    incident = _incident(sqlite_factory)
    key = _key(incident, _a1("u1"))
    _revise(sqlite_factory, incident, _diagnosis(_a1("u1")))
    _, transitions = _revise(
        sqlite_factory,
        incident,
        _diagnosis(_a1("u1", PreconditionResult.disqualified("POST_ONSET_READY_FALSE"))),
    )
    assert transitions == RequirementTransitions(satisfied=(key,))
    assert _open(sqlite_factory, incident) == []


@pytest.mark.parametrize(
    "reason",
    [
        RequirementAuditReason.NO_DATA_AFTER_DEADLINE,
        RequirementAuditReason.PARTIAL_COVERAGE_AFTER_DEADLINE,
    ],
)
def test_deadline_gaps_leave_the_open_row_untouched(
    sqlite_factory: sessionmaker[Session], reason: RequirementAuditReason
) -> None:
    incident = _incident(sqlite_factory)
    key = _key(incident, _a2())
    first, _ = _revise(sqlite_factory, incident, _diagnosis(_a2()))
    _, transitions = _revise(sqlite_factory, incident, _diagnosis(_a2(audit_reason=reason)))
    assert transitions == RequirementTransitions(unchanged=(key,))
    # Missing is never "satisfied": the same row stays OPEN, nothing new is written.
    assert _rows(sqlite_factory, incident) == [(key, "OPEN", first, "RESOURCE_COVERAGE")]


def test_scope_exit_supersedes_without_reopening(sqlite_factory: sessionmaker[Session]) -> None:
    incident = _incident(sqlite_factory)
    key = _key(incident, _a2())
    _revise(sqlite_factory, incident, _diagnosis(_a2()))
    _, transitions = _revise(
        sqlite_factory, incident, _diagnosis(_a2(audit_reason=RequirementAuditReason.SCOPE_EXITED))
    )
    assert transitions == RequirementTransitions(superseded=(key,))
    assert _open(sqlite_factory, incident) == []


def test_hypothesis_absent_from_the_full_inventory_supersedes(
    sqlite_factory: sessionmaker[Session],
) -> None:
    incident = _incident(sqlite_factory)
    key = _key(incident, _a2())
    _revise(sqlite_factory, incident, _diagnosis(_a2()))
    _, transitions = _revise(sqlite_factory, incident, _diagnosis(keys=("hkey:someone-else",)))
    assert transitions == RequirementTransitions(superseded=(key,))
    assert _open(sqlite_factory, incident) == []


def test_ambiguous_or_missing_identity_leaves_open_rows_and_writes_nothing(
    sqlite_factory: sessionmaker[Session],
) -> None:
    incident = _incident(sqlite_factory)
    key = _key(incident, _a2())
    first, _ = _revise(sqlite_factory, incident, _diagnosis(_a2()))
    # The key now repeats in the inventory: even a PASS for it is not matched.
    _, repeated = _revise(
        sqlite_factory,
        incident,
        _diagnosis(_a2(result=PreconditionResult.passed()), keys=("hkey:payment", "hkey:payment")),
    )
    assert repeated == RequirementTransitions(unchanged=(key,))
    # A new PENDING with a repeated or missing key creates no row.
    _, fresh = _revise(
        sqlite_factory,
        incident,
        _diagnosis(
            _a2(),
            _a1("u9", key="hkey:dup"),
            _a1("u9", hypothesis_key=None, key="hkey:none"),
            keys=("hkey:payment", "hkey:payment", "hkey:dup", "hkey:dup", None),
        ),
    )
    assert fresh == RequirementTransitions(unchanged=(key,))
    # Hypothesis still present and unique, but no evaluation this revision: untouched.
    _, silent = _revise(sqlite_factory, incident, _diagnosis(keys=("hkey:payment",)))
    assert silent == RequirementTransitions(unchanged=(key,))
    assert _rows(sqlite_factory, incident) == [(key, "OPEN", first, "RESOURCE_COVERAGE")]


def test_each_uid_is_an_independent_obligation(sqlite_factory: sessionmaker[Session]) -> None:
    incident = _incident(sqlite_factory)
    uid_a, uid_b = _key(incident, _a1("uid-a")), _key(incident, _a1("uid-b"))
    _, first = _revise(sqlite_factory, incident, _diagnosis(_a1("uid-a")))
    assert first == RequirementTransitions(opened=(uid_a,))
    # Reincarnation: uid-a ended (its requirement passes) while uid-b is new and pending.
    _, second = _revise(
        sqlite_factory,
        incident,
        _diagnosis(_a1("uid-a", PreconditionResult.passed()), _a1("uid-b")),
    )
    assert second == RequirementTransitions(opened=(uid_b,), satisfied=(uid_a,))
    # Both pending at once is legitimate: two keys, two OPEN rows.
    _, third = _revise(sqlite_factory, incident, _diagnosis(_a1("uid-b"), _a1("uid-c")))
    uid_c = _key(incident, _a1("uid-c"))
    assert third == RequirementTransitions(opened=(uid_b, uid_c), superseded=(uid_b,))
    assert sorted(_open(sqlite_factory, incident)) == sorted([uid_b, uid_c])


def test_a_changed_series_is_a_new_obligation_and_never_fuzzy_closes_the_old(
    sqlite_factory: sessionmaker[Session],
) -> None:
    incident = _incident(sqlite_factory)
    memory = _key(incident, _a2())
    both = _key(incident, _a2((("app", "cpu"), ("app", "memory"))))
    _revise(sqlite_factory, incident, _diagnosis(_a2()))
    _, transitions = _revise(
        sqlite_factory, incident, _diagnosis(_a2((("app", "cpu"), ("app", "memory"))))
    )
    # Same hypothesis and rule, different key: the old OPEN is not matched.
    assert transitions == RequirementTransitions(opened=(both,), unchanged=(memory,))
    assert sorted(_open(sqlite_factory, incident)) == sorted([memory, both])


def test_other_incidents_are_never_touched(sqlite_factory: sessionmaker[Session]) -> None:
    first, second = _incident(sqlite_factory), _incident(sqlite_factory)
    _revise(sqlite_factory, first, _diagnosis(_a2()))
    _revise(sqlite_factory, second, _diagnosis(keys=()))
    assert _open(sqlite_factory, first) == [_key(first, _a2())]
    assert _key(first, _a2()) != _key(second, _a2())


# --- same transaction ---------------------------------------------------------


def _counts(factory: sessionmaker[Session]) -> tuple[int, int]:
    with factory() as session:
        return (
            session.scalar(select(func.count()).select_from(DiagnosisRow)) or 0,
            session.scalar(select(func.count()).select_from(EvidenceRequirementRow)) or 0,
        )


def test_a_failing_companion_rolls_back_the_diagnosis_and_its_requirements(
    sqlite_factory: sessionmaker[Session],
) -> None:
    incident = _incident(sqlite_factory)
    _revise(sqlite_factory, incident, _diagnosis(_a2()))
    before = _counts(sqlite_factory)

    def fail(_: Session) -> None:
        raise RuntimeError("companion failed after the requirement writes")

    with pytest.raises(RuntimeError, match="companion failed"):
        _revise(sqlite_factory, incident, _diagnosis(_a2()), after=fail)
    assert _counts(sqlite_factory) == before
    with pytest.raises(ValueError, match="conflicting evaluations"):
        _revise(
            sqlite_factory,
            incident,
            _diagnosis(_a2(), _a2(result=PreconditionResult.passed())),
        )
    assert _counts(sqlite_factory) == before
    assert _open(sqlite_factory, incident) == [_key(incident, _a2())]
    with sqlite_factory() as session:
        (only,) = DiagnosisRepository(session).list_revisions(incident)
        assert only.revision_number == 1  # failed attempts consumed no number


# --- service path -------------------------------------------------------------


def test_every_service_revision_applies_the_lifecycle(
    setup: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, cluster, clock, incident_id = setup
    real = engine_diagnose
    pending = _a2()

    def with_requirement(*args: Any, **kwargs: Any) -> Diagnosis:
        diagnosis = real(*args, **kwargs)
        return diagnosis.model_copy(
            update={
                "requirement_evaluations": (pending,),
                "hypothesis_inventory": (
                    *diagnosis.hypothesis_inventory,
                    HypothesisInventoryEntry(
                        hypothesis_id="hypothesis:payment", hypothesis_key="hkey:payment"
                    ),
                ),
            }
        )

    monkeypatch.setattr(diagnosis_module, "diagnose", with_requirement)
    service = DiagnosisService(
        session_factory=factory, namespaces=("sre-demo",), reader=cluster, clock=clock
    )
    clock.now = T0 + timedelta(minutes=20)
    service.run(incident_id, "INITIAL")
    clock.now = T0 + timedelta(minutes=25)
    service.run(incident_id, "MANUAL")

    key = _key(incident_id, pending)
    with factory() as session:
        revisions = DiagnosisRepository(session).list_revisions(incident_id)
        rows = EvidenceRequirementRepository(session).for_incident(incident_id)
        stored = [
            RequirementEvaluation.model_validate(item)
            for item in revisions[-1].document["requirement_evaluations"]
        ]
    assert [(row.requirement_key, row.status) for row in rows] == [
        (key, "SUPERSEDED_BY_REVISION"),
        (key, "OPEN"),
    ]
    assert [row.diagnosis_id for row in rows] == [r.diagnosis_id for r in revisions[-2:]]
    assert stored == [pending]  # the rows follow from the persisted revision document
