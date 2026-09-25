"""M19-4.4: revision history API over persisted diagnosis rows."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from apps.control_plane.diagnosis import DiagnosisService
from apps.control_plane.main import create_app
from apps.control_plane.schemas import RevisionDiffResponse
from packages.contracts import Incident, IncidentSeverity, IncidentSource, IncidentStatus
from packages.rca.model import Confidence, Diagnosis, Resolution, Symptoms
from packages.rca.revision_diff import PersistedDiagnosisRevision, diff_revisions
from packages.storage.database import create_session_factory
from packages.storage.models import Base, DiagnosisRow
from packages.storage.repositories import DiagnosisRepository, IncidentRepository

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _diagnosis(incident_id: UUID, resolution: Resolution, summary: str) -> dict[str, Any]:
    return Diagnosis(
        incident_id=str(incident_id),
        root_cause=None,
        confidence=Confidence.UNVERIFIED,
        resolution=resolution,
        summary=summary,
        symptoms=Symptoms(
            onset=NOW,
            last_seen=NOW,
            services=("api",),
            namespaces=("shop",),
            alert_names=(),
        ),
    ).model_dump(mode="json")


def _row(
    incident_id: UUID,
    revision: int,
    *,
    diagnosis_id: int,
    previous: int | None = None,
    created_at: datetime | None = None,
    trigger: str = "MANUAL",
    resolution: Resolution = Resolution.AMBIGUOUS,
    manifest_digest: str | None = "manifest:one",
    provenance: bool = True,
) -> DiagnosisRow:
    return DiagnosisRow(
        diagnosis_id=diagnosis_id,
        incident_id=incident_id,
        created_at=created_at or NOW + timedelta(minutes=revision),
        root_cause=None,
        confidence="UNVERIFIED",
        mode="deterministic",
        run_id=f"run-{diagnosis_id}" if provenance else None,
        document=_diagnosis(incident_id, resolution, f"stored-{diagnosis_id}"),
        revision_number=revision,
        previous_diagnosis_id=previous,
        trigger=trigger,
        window_end=NOW if provenance else None,
        manifest_digest=manifest_digest if provenance else None,
        tape_digest=f"tape-{diagnosis_id}" if provenance else None,
        epistemic_digest=f"epistemic-{diagnosis_id}" if provenance else None,
        engine_version="1.1.2" if provenance else None,
        config_digest=f"config-{diagnosis_id}" if provenance else None,
    )


def _setup(tmp_path: Path) -> tuple[Any, Any, UUID, DiagnosisService]:
    engine = create_engine(f"sqlite:///{tmp_path / 'diagnosis-revision-api.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    incident = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.SYSTEM,
        title="Revision API incident",
        created_at=NOW,
        updated_at=NOW,
    )
    with Session(engine) as session:
        IncidentRepository(session).create(incident)
    service = DiagnosisService(session_factory=factory, namespaces=("sre-demo",))
    return engine, factory, incident.incident_id, service


def test_post_defaults_to_manual_and_rejects_internal_triggers(
    tmp_path: Path,
) -> None:
    engine, factory, incident_id, _service = _setup(tmp_path)

    class PersistingDiagnoser:
        reader = None

        def run(self, requested_incident: UUID, trigger: str) -> Diagnosis:
            assert requested_incident == incident_id
            document = _diagnosis(incident_id, Resolution.AMBIGUOUS, f"post-{trigger}")
            with factory() as session:
                DiagnosisRepository(session).save_revision(
                    incident_id=incident_id,
                    document=document,
                    created_at=NOW,
                    run_id=str(uuid4()),
                    trigger=trigger,
                    window_end=NOW,
                    manifest_digest="manifest",
                    tape_digest="tape",
                    epistemic_digest="epistemic",
                    engine_version="1.1.2",
                    config_digest="config",
                )
            return Diagnosis.model_validate(document)

    service = PersistingDiagnoser()
    with TestClient(create_app(factory, diagnosis_service=service)) as client:  # type: ignore[arg-type]
        omitted = client.post(f"/api/v1/incidents/{incident_id}/diagnosis")
        explicit = client.post(f"/api/v1/incidents/{incident_id}/diagnosis?trigger=MANUAL")
        assert omitted.status_code == explicit.status_code == 200
        assert omitted.json()["incident_id"] == str(incident_id)
        assert explicit.json()["incident_id"] == str(incident_id)
        for trigger in ("INITIAL", "EVIDENCE_DEADLINE", "LEGACY"):
            rejected = client.post(f"/api/v1/incidents/{incident_id}/diagnosis?trigger={trigger}")
            assert rejected.status_code == 422
    with Session(engine) as session:
        rows = session.scalars(
            select(DiagnosisRow).where(DiagnosisRow.incident_id == incident_id)
        ).all()
        assert [row.revision_number for row in rows] == [1, 2]
        assert [row.trigger for row in rows] == ["MANUAL", "MANUAL"]
    engine.dispose()


def test_empty_revision_collection_and_missing_revision_follow_read_conventions(
    tmp_path: Path,
) -> None:
    engine, factory, incident_id, _service = _setup(tmp_path)

    class NoLiveCalls:
        reader = None

        def run(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("persisted revision GET must not execute diagnosis")

    with TestClient(create_app(factory, diagnosis_service=NoLiveCalls())) as client:  # type: ignore[arg-type]
        assert client.get(f"/api/v1/incidents/{incident_id}/diagnoses").json() == []
        missing_revision = client.get(f"/api/v1/incidents/{incident_id}/diagnoses/1")
        assert missing_revision.status_code == 404
        assert missing_revision.json()["error"]["code"] == "DIAGNOSIS_NOT_FOUND"
        missing_latest = client.get(f"/api/v1/incidents/{incident_id}/diagnosis")
        assert missing_latest.status_code == 404
        assert missing_latest.json()["error"]["code"] == "DIAGNOSIS_NOT_FOUND"
        assert client.get(f"/api/v1/incidents/{UUID(int=1)}/diagnoses").json() == []
    engine.dispose()


def test_revision_list_is_incident_scoped_ordered_and_uses_persisted_metadata(
    tmp_path: Path,
) -> None:
    engine, factory, incident_a, service = _setup(tmp_path)
    incident_b = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.SYSTEM,
        title="Other revision API incident",
        created_at=NOW,
        updated_at=NOW,
    )
    with Session(engine) as session:
        IncidentRepository(session).create(incident_b)
        session.add_all(
            [
                _row(incident_a, 3, diagnosis_id=103, previous=102, created_at=NOW),
                _row(incident_a, 1, diagnosis_id=101, created_at=NOW + timedelta(days=1)),
                _row(
                    incident_a,
                    2,
                    diagnosis_id=102,
                    previous=101,
                    created_at=NOW - timedelta(days=1),
                ),
                _row(incident_b.incident_id, 1, diagnosis_id=100),
                _row(incident_a, 4, diagnosis_id=104, previous=103, manifest_digest="manifest:two"),
            ]
        )
        session.commit()

    with TestClient(create_app(factory, diagnosis_service=service)) as client:
        listed = client.get(f"/api/v1/incidents/{incident_a}/diagnoses")
        assert listed.status_code == 200
        body = listed.json()
        assert [item["revision_number"] for item in body] == [1, 2, 3, 4]
        assert [item["diagnosis_id"] for item in body] == [101, 102, 103, 104]
        assert body[1]["previous_diagnosis_id"] == 101
        assert body[1]["trigger"] == "MANUAL"
        assert body[1]["manifest_digest"] == "manifest:one"
        assert body[1]["tape_digest"] == "tape-102"
        assert body[1]["epistemic_digest"] == "epistemic-102"
        assert body[1]["engine_version"] == "1.1.2"
        assert body[1]["config_digest"] == "config-102"
        assert (
            client.get(f"/api/v1/incidents/{incident_a}/diagnoses/1").json()["diagnosis_id"] == 101
        )
        assert (
            client.get(f"/api/v1/incidents/{incident_a}/diagnoses/2").json()["diagnosis_id"] == 102
        )  # n is revision_number, not diagnosis_id.
        assert client.get(f"/api/v1/incidents/{incident_a}/diagnoses/1").json()["diff"] is None
        same_manifest = client.get(f"/api/v1/incidents/{incident_a}/diagnoses/2").json()
        assert same_manifest["diff"]["manifest_diff"] == {
            "previous_digest": "manifest:one",
            "current_digest": "manifest:one",
            "status": "UNCHANGED",
        }
        changed_manifest = client.get(f"/api/v1/incidents/{incident_a}/diagnoses/4").json()
        assert changed_manifest["diff"]["manifest_diff"] == {
            "previous_digest": "manifest:one",
            "current_digest": "manifest:two",
            "status": "CHANGED",
        }
        assert client.get(f"/api/v1/incidents/{incident_a}/diagnoses/99").status_code == 404
        assert (
            client.get(f"/api/v1/incidents/{incident_a}/diagnoses/99").json()["error"]["code"]
            == "DIAGNOSIS_NOT_FOUND"
        )
    engine.dispose()


def test_detail_uses_only_persisted_previous_pointer_and_matches_pure_diff(
    tmp_path: Path,
) -> None:
    engine, factory, incident_id, service = _setup(tmp_path)
    previous_doc = _diagnosis(incident_id, Resolution.AMBIGUOUS, "previous")
    current_doc = _diagnosis(incident_id, Resolution.RESOLVED, "current")
    with Session(engine) as session:
        previous = _row(
            incident_id,
            7,
            diagnosis_id=907,
            trigger="LEGACY",
            resolution=Resolution.AMBIGUOUS,
            provenance=False,
        )
        previous.document = previous_doc
        current = _row(
            incident_id,
            12,
            diagnosis_id=912,
            previous=907,
            trigger="MANUAL",
            resolution=Resolution.RESOLVED,
        )
        current.document = current_doc
        session.add_all([previous, current])
        session.commit()

    expected = diff_revisions(
        PersistedDiagnosisRevision(previous_doc, None),
        PersistedDiagnosisRevision(current_doc, "manifest:one"),
    )
    with TestClient(create_app(factory, diagnosis_service=service)) as client:
        detail_response = client.get(f"/api/v1/incidents/{incident_id}/diagnoses/12")
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["diagnosis_id"] == 912
        assert detail["revision_number"] == 12
        assert detail["previous_diagnosis_id"] == 907
        assert detail["diagnosis"] == current_doc
        expected_document = asdict(expected)
        expected_document["resolution_transition"]["changed"] = (
            expected.resolution_transition.changed
        )
        expected_response = RevisionDiffResponse.model_validate(expected_document)
        assert detail["diff"] == expected_response.model_dump(mode="json")
        assert detail["diff"]["manifest_diff"]["status"] == "UNKNOWN"
    engine.dispose()


def test_legacy_null_provenance_and_unlinked_revision_have_null_diff(
    tmp_path: Path, monkeypatch: Any
) -> None:
    engine, factory, incident_id, _service = _setup(tmp_path)
    with Session(engine) as session:
        session.add_all(
            [
                _row(incident_id, 1, diagnosis_id=301, trigger="LEGACY", provenance=False),
                _row(incident_id, 4, diagnosis_id=304, trigger="LEGACY", provenance=False),
            ]
        )
        session.commit()

    class NoLiveCalls:
        reader = None

        def run(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("revision GET must not execute diagnosis")

    monkeypatch.setenv("SRE_API_TOKEN", "read-token")
    with TestClient(create_app(factory, diagnosis_service=NoLiveCalls())) as client:  # type: ignore[arg-type]
        listing = client.get(f"/api/v1/incidents/{incident_id}/diagnoses")
        assert listing.status_code == 200
        assert listing.json()[0]["run_id"] is None
        assert listing.json()[0]["window_end"] is None
        assert listing.json()[0]["manifest_digest"] is None
        assert listing.json()[0]["tape_digest"] is None
        assert listing.json()[0]["epistemic_digest"] is None
        assert listing.json()[0]["engine_version"] is None
        assert listing.json()[0]["config_digest"] is None
        assert client.get(f"/api/v1/incidents/{incident_id}/diagnoses/4").json()["diff"] is None
        assert client.get(f"/api/v1/incidents/{incident_id}/diagnoses/1").json()["diff"] is None
    engine.dispose()


def test_previous_pointer_cannot_cross_incident_and_latest_route_is_unchanged(
    tmp_path: Path,
) -> None:
    engine, factory, incident_a, service = _setup(tmp_path)
    incident_b = Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.SYSTEM,
        title="Cross-incident pointer target",
        created_at=NOW,
        updated_at=NOW,
    )
    with Session(engine) as session:
        IncidentRepository(session).create(incident_b)
        linked_elsewhere = _row(incident_b.incident_id, 1, diagnosis_id=401)
        same_number_elsewhere = _row(incident_b.incident_id, 2, diagnosis_id=400)
        current = _row(incident_a, 2, diagnosis_id=402, previous=401)
        session.add_all([same_number_elsewhere, linked_elsewhere, current])
        session.commit()

    with TestClient(
        create_app(factory, diagnosis_service=service), raise_server_exceptions=False
    ) as client:
        broken = client.get(f"/api/v1/incidents/{incident_a}/diagnoses/2")
        assert broken.status_code == 500

        # Legacy latest endpoint still returns only its old raw document shape
        # and selects by created_at, even when revision-number order differs.
        old_latest = _row(incident_a, 1, diagnosis_id=403, created_at=NOW + timedelta(days=3))
        old_latest_document = old_latest.document
        with Session(engine) as session:
            session.add(old_latest)
            session.commit()
        latest = client.get(f"/api/v1/incidents/{incident_a}/diagnosis")
        assert latest.status_code == 200
        assert latest.json() == old_latest_document
        assert set(latest.json()) == set(old_latest_document)
    engine.dispose()
