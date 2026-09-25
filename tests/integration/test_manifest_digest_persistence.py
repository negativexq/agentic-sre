"""M19-3.7: manifest membership digest survives persistence and reload."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from packages.rca.manifest import manifest_membership_digest
from packages.storage.database import create_session_factory
from packages.storage.manifest import load_manifest_digest
from packages.storage.models import Base, RunEvidenceManifestRow


@contextmanager
def _manifest_store(url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def test_manifest_digest_reloads_membership_and_ignores_storage_fields(tmp_path: Any) -> None:
    membership = (
        ("OBJECT_VERSION", "10"),
        ("EVENT_VERSION", "20"),
        ("ALERT", "30"),
    )
    expected = manifest_membership_digest(membership)

    with _manifest_store(f"sqlite:///{tmp_path / 'manifest-digest.db'}") as factory:
        with factory() as session:
            session.add_all(
                [
                    RunEvidenceManifestRow(
                        run_id="digest-run-a",
                        sequence=sequence,
                        source_type=source_type,
                        source_id=source_id,
                        payload=payload,
                    )
                    for sequence, source_type, source_id, payload in (
                        (90, "OBJECT_VERSION", "10", {"body": "one"}),
                        (4, "EVENT_VERSION", "20", {"body": "two"}),
                        (25, "ALERT", "30", {"status": "FIRING"}),
                    )
                ]
            )
            session.add_all(
                [
                    RunEvidenceManifestRow(
                        run_id="digest-run-b",
                        sequence=sequence,
                        source_type=source_type,
                        source_id=source_id,
                        payload=payload,
                    )
                    for sequence, source_type, source_id, payload in (
                        (2, "ALERT", "30", {"status": "RESOLVED"}),
                        (120, "OBJECT_VERSION", "10", {"body": "changed"}),
                        (37, "EVENT_VERSION", "20", {"body": "changed"}),
                    )
                ]
            )
            session.commit()

        with factory() as reloaded:
            rows_a = reloaded.scalars(
                select(RunEvidenceManifestRow).where(
                    RunEvidenceManifestRow.run_id == "digest-run-a"
                )
            ).all()
            rows_b = reloaded.scalars(
                select(RunEvidenceManifestRow).where(
                    RunEvidenceManifestRow.run_id == "digest-run-b"
                )
            ).all()
        by_membership_a = {(row.source_type, row.source_id): row for row in rows_a}
        by_membership_b = {(row.source_type, row.source_id): row for row in rows_b}
        assert by_membership_a.keys() == by_membership_b.keys()
        assert all(
            by_membership_a[key].manifest_entry_id != by_membership_b[key].manifest_entry_id
            and by_membership_a[key].sequence != by_membership_b[key].sequence
            and by_membership_a[key].payload != by_membership_b[key].payload
            for key in by_membership_a
        )

        # A separate session proves the digest is recomputable from persisted rows.
        with factory() as reloaded:
            digest_a = load_manifest_digest(reloaded, "digest-run-a")
        with factory() as reloaded:
            digest_b = load_manifest_digest(reloaded, "digest-run-b")

    assert digest_a == expected
    assert digest_b == expected
    assert digest_a == digest_b
