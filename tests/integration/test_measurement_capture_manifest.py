"""Transport capture round trip has real manifest members and no fabricated RCA."""

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.evals.frozen_request_binding import FrozenRequestEvidence, corroborate_frozen_requests
from packages.evals.measurement_capture_manifest import freeze_measurement_capture
from packages.storage.models import DiagnosisRow, RunEvidenceManifestRow


def test_measurement_manifest_preserves_relations_without_creating_diagnosis(
    tmp_path: Path,
) -> None:
    fixture = (
        Path(__file__).resolve().parents[1] / "unit/evals/fixtures/frozen_request_evidence.json"
    )
    original = FrozenRequestEvidence.model_validate_json(fixture.read_text())
    path = tmp_path / "capture.sqlite"
    loaded, digest = freeze_measurement_capture(path, original)
    assert len(digest) == 64
    assert corroborate_frozen_requests(loaded).relations[0].request_relation == "CORROBORATED"
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        assert session.scalars(select(DiagnosisRow)).all() == []
        members = session.scalars(select(RunEvidenceManifestRow)).all()
        assert len(members) == 3
        references = {(row.source_type, row.source_id) for row in members}
        for item in (*loaded.logs, *loaded.spans):
            _, _, kind, row_id = item.source_reference.split(":")
            assert (kind, row_id) in references
    engine.dispose()
