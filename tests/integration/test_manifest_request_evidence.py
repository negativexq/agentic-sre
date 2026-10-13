"""Exact member/digest scope with legacy schemas and read-only enforcement."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.evals.frozen_request_binding import FrozenRequestEvidence
from packages.evals.manifest_request_evidence import (
    FrozenSourceIntegrityError,
    load_manifest_request_evidence,
)
from packages.rca.manifest import manifest_membership_digest
from packages.storage.models import (
    Base,
    DiagnosisRow,
    IncidentEventRow,
    IncidentRow,
    LogObservationRow,
    RunEvidenceManifestRow,
    TraceObservationRow,
)


@pytest.fixture
def database() -> Any:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    fixture = (
        Path(__file__).resolve().parents[1] / "unit/evals/fixtures/frozen_request_evidence.json"
    )
    evidence = FrozenRequestEvidence.model_validate(json.loads(fixture.read_text()))
    incident_id = uuid4()
    at = datetime(2026, 10, 13, 15, 3, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            IncidentRow(
                incident_id=incident_id,
                status="OPEN",
                severity="HIGH",
                source="ALERT",
                title="fixture",
                created_at=at,
                updated_at=at,
                correlation_id=uuid4(),
            )
        )
        session.flush()
        log = evidence.logs[0].record
        session.add(
            LogObservationRow(
                observation_id=1,
                incident_id=incident_id,
                service=log.service,
                event_at=log.at,
                observed_at=evidence.logs[0].captured_at,
                severity=log.severity,
                message=log.message,
                evidence_id=log.evidence_id,
                dedup_key="log",
                attributes=log.attributes,
            )
        )
        for index, item in enumerate(evidence.spans, 1):
            session.add(
                TraceObservationRow(
                    observation_id=index,
                    incident_id=incident_id,
                    service=item.record.service,
                    event_at=item.record.start_at,
                    observed_at=item.captured_at,
                    span=item.record.model_dump(mode="json"),
                    dedup_key=f"span:{index}",
                )
            )
        entries = [("LOG", "1"), ("TRACE", "1"), ("TRACE", "2")]
        for sequence, (kind, sid) in enumerate(entries, 1):
            session.add(
                RunEvidenceManifestRow(
                    run_id="run", sequence=sequence, source_type=kind, source_id=sid
                )
            )
        session.add(
            DiagnosisRow(
                incident_id=incident_id,
                created_at=at,
                confidence="LOW",
                mode="CAUSAL",
                run_id="run",
                document={"resolution": "AMBIGUOUS"},
                revision_number=1,
                trigger="INITIAL",
                manifest_digest=manifest_membership_digest(entries),
            )
        )
        session.add(
            IncidentEventRow(
                event_id=uuid4(),
                incident_id=incident_id,
                sequence=1,
                event_type="EVIDENCE_GATHERED",
                timestamp=at,
                correlation_id=uuid4(),
                payload={
                    "run_id": "run",
                    "window_end": at.isoformat(),
                    "snapshot_cycle_id": None,
                    "objects": 0,
                    "provider_capabilities": ["logs", "runtime_traces"],
                },
            )
        )
        session.commit()
    yield engine, evidence
    engine.dispose()


def read(engine: Engine, evidence: FrozenRequestEvidence) -> Any:
    with engine.connect() as connection:
        connection.execute(text("pragma query_only=on"))
        try:
            return load_manifest_request_evidence(
                connection,
                run_id="run",
                episode_start=evidence.episode_start,
                episode_end=evidence.episode_end,
                caller_service=evidence.caller_service,
                configured_peer=evidence.configured_peer,
            )
        finally:
            connection.execute(text("pragma query_only=off"))


def test_only_exact_manifest_members_and_recorded_digest_are_used(database: Any) -> None:
    engine, evidence = database
    with engine.begin() as connection:
        # Unmanifested rows do not enter the export, even when they share an evidence ID.
        connection.execute(
            text(
                "insert into log_observations (observation_id,incident_id,service,event_at,observed_at,severity,message,evidence_id,dedup_key,source_system) select 99,incident_id,service,event_at,observed_at,severity,'newer decoy',evidence_id,'decoy','loki' from log_observations where observation_id=1"
            )
        )
    loaded = read(engine, evidence)
    assert len(loaded.evidence.logs) == 1
    report = loaded.report()
    assert report["report"]["manifest_membership"] == "VERIFIED_AGAINST_RECORDED_DIGEST"
    assert report["report"]["relations"][0]["request_relation"] == "CORROBORATED"
    assert report["report"]["source_authenticity"] == "NOT_VERIFIED"
    with Session(engine) as session:
        assert session.query(DiagnosisRow).one().document == {"resolution": "AMBIGUOUS"}


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_member",
        "wrong_digest",
        "wrong_incident",
        "late_capture",
        "trace_index_mismatch",
        "missing_recorded_digest",
    ],
)
def test_inconsistent_members_cannot_fall_back_to_current_rows(
    database: Any, mutation: str
) -> None:
    engine, evidence = database
    with engine.begin() as connection:
        if mutation == "missing_member":
            connection.execute(
                delete(TraceObservationRow).where(TraceObservationRow.observation_id == 1)
            )
        elif mutation == "wrong_digest":
            connection.execute(update(DiagnosisRow).values(manifest_digest="0" * 64))
        elif mutation == "wrong_incident":
            connection.execute(update(LogObservationRow).values(incident_id=uuid4()))
        elif mutation == "late_capture":
            connection.execute(
                update(LogObservationRow).values(observed_at=datetime(2026, 10, 13, 16, tzinfo=UTC))
            )
        elif mutation == "trace_index_mismatch":
            connection.execute(update(TraceObservationRow).values(service="decoy"))
        else:
            connection.execute(update(DiagnosisRow).values(manifest_digest=None))
    with pytest.raises(FrozenSourceIntegrityError):
        read(engine, evidence)


def test_legacy_schema_has_empty_assertions_without_migration(database: Any) -> None:
    engine, evidence = database
    with engine.begin() as connection:
        connection.execute(text("alter table log_observations drop column attributes"))
    loaded = read(engine, evidence)
    assert loaded.log_attributes_column_present is False
    assert loaded.evidence.logs[0].record.attributes == {}
    assert loaded.report()["report"]["relations"][0]["request_relation"] == "UNASSESSED"


def test_write_capable_connection_is_rejected_before_loading(database: Any) -> None:
    engine, evidence = database
    with (
        engine.connect() as connection,
        pytest.raises(FrozenSourceIntegrityError, match="read-only"),
    ):
        load_manifest_request_evidence(
            connection,
            run_id="run",
            episode_start=evidence.episode_start,
            episode_end=evidence.episode_end,
            caller_service="order",
            configured_peer="payment",
        )


def test_explicit_scope_cannot_exceed_recorded_boundary(database: Any) -> None:
    engine, evidence = database
    extended = evidence.model_copy(update={"episode_end": datetime(2026, 10, 13, 16, tzinfo=UTC)})
    with pytest.raises(FrozenSourceIntegrityError, match="interval"):
        read(engine, extended)


def test_no_diagnosis_or_provider_invoked(database: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import packages.rca.engine as engine_module
    import packages.rca.live as live

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Manifest evidence reader cannot diagnose or call providers")

    monkeypatch.setattr(engine_module, "diagnose_case", forbidden)
    monkeypatch.setattr(engine_module, "build_case", forbidden)
    monkeypatch.setattr(live.LokiLogReader, "error_logs", forbidden)
    engine, evidence = database
    assert (
        read(engine, evidence).report()["report"]["relations"][0]["request_relation"]
        == "CORROBORATED"
    )
