"""The Connector registry in the control plane's database (connector-install-design.md §A8.2)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.connector.enrollment import ConnectorRecord, Status
from packages.storage.models import ConnectorRow


def _record(row: ConnectorRow) -> ConnectorRecord:
    status: Status = row.status  # type: ignore[assignment]
    return ConnectorRecord(
        connector_id=row.connector_id,
        status=status,
        created_at=row.created_at,
        token_hash=row.token_hash,
        token_expires_at=row.token_expires_at,
        token_used_at=row.token_used_at,
        cert_serial=row.cert_serial,
        cert_not_after=row.cert_not_after,
    )


class SqlRegistryStore:
    """``RegistryStore`` over the ``connectors`` table."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def get(self, connector_id: str) -> ConnectorRecord | None:
        with self._sessions() as session:
            row = session.get(ConnectorRow, connector_id)
            return _record(row) if row is not None else None

    def put(self, record: ConnectorRecord) -> None:
        with self._sessions() as session:
            session.merge(
                ConnectorRow(
                    connector_id=record.connector_id,
                    status=record.status,
                    created_at=record.created_at,
                    token_hash=record.token_hash,
                    token_expires_at=record.token_expires_at,
                    token_used_at=record.token_used_at,
                    cert_serial=record.cert_serial,
                    cert_not_after=record.cert_not_after,
                )
            )
            session.commit()

    def all(self) -> list[ConnectorRecord]:
        with self._sessions() as session:
            rows = session.scalars(select(ConnectorRow).order_by(ConnectorRow.connector_id))
            return [_record(row) for row in rows]
