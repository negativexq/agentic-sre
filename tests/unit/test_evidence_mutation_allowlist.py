"""M19-2.8: authoritative evidence is append-only, statically and at runtime.

The static scan reads every product-path Python source for UPDATE / DELETE /
TRUNCATE against the evidence tables, in raw SQL and as SQLAlchemy
``update()`` / ``delete()`` calls. Only the §1.5 exceptions listed here, each
narrowed to one file, operation and table, may appear. The runtime guard
covers ORM attribute changes and ``session.delete()``, which no scan sees.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.storage.evidence_guard import (
    AUTHORITATIVE_ROWS,
    TRANSITIONAL_MUTABLE_ROWS,
    AuthoritativeEvidenceMutation,
)
from packages.storage.models import (
    AlertRow,
    Base,
    EntityInstanceRow,
    IncidentRow,
    LifecycleObservationRow,
    ObjectVersionRow,
)

ROOT = Path(__file__).resolve().parents[2]

# Authoritative evidence tables and their ORM classes, written out explicitly.
EVIDENCE_TABLES = {
    "alerts": "AlertRow",
    "change_records": "ChangeRecordRow",
    "event_versions": "EventVersionRow",
    "lifecycle_observations": "LifecycleObservationRow",
    "log_observations": "LogObservationRow",
    "object_versions": "ObjectVersionRow",
}
ROW_TABLES = {row: table for table, row in EVIDENCE_TABLES.items()}

# Not product paths: tests, and migrations (schema changes and body backfills,
# e.g. 0016 filling object_versions.uid). Excluding them loosens nothing at
# runtime: product sessions stay guarded.
EXCLUDED = ("tests/", "alembic/versions/", ".venv/", ".local/", "node_modules/")

# §1.5 exceptions, each one file + operation + table ("<dynamic>" when the
# statement builds its table list at runtime).
ALLOWED = {
    ("packages/storage/retention.py", "DELETE", "event_versions"),
    ("packages/storage/retention.py", "DELETE", "lifecycle_observations"),
    ("packages/storage/retention.py", "DELETE", "object_versions"),
    # Legacy live runner resets its journal between frozen scenarios.
    ("packages/evals/live/actions.py", "TRUNCATE", "<dynamic>"),
}

_RAW_SQL = re.compile(
    r"\b(?:(?P<update>UPDATE)\s+\"?(?P<u>\w+)\"?\s+SET\b"
    r"|(?P<delete>DELETE)\s+FROM\s+\"?(?P<d>\w+)"
    r"|(?P<truncate>TRUNCATE)(?:\s+TABLE)?\s+(?P<t>[\w\", ]*))"
)


def _string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(part.value) if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
    return None


def _raw_sql(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in _RAW_SQL.finditer(text):
        if match["update"]:
            operation, targets = "UPDATE", [match["u"]]
        elif match["delete"]:
            operation, targets = "DELETE", [match["d"]]
        else:
            operation = "TRUNCATE"
            targets = [item.strip('" ') for item in match["t"].split(",") if item.strip('" ')]
        tables = [table for table in targets if table in EVIDENCE_TABLES]
        if tables:
            found += [(operation, table) for table in tables]
        elif operation == "TRUNCATE" and not targets:
            found.append((operation, "<dynamic>"))
    return found


def _row_table(node: ast.AST) -> str | None:
    """The evidence table an ``update(X)``/``delete(X)`` target names, if any."""
    if isinstance(node, ast.Attribute) and node.attr == "__table__":
        node = node.value
    name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", None)
    return ROW_TABLES.get(name) if isinstance(name, str) else None


def violations(source: str, path: str) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for node in ast.walk(ast.parse(source, path)):
        text = _string(node)
        if text is not None:
            found |= {(path, operation, table) for operation, table in _raw_sql(text)}
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in {"update", "delete"}:
                table = _row_table(node.args[0])
                if table is not None:
                    found.add((path, name.upper(), table))
    return found


def _product_sources() -> list[Path]:
    return [
        path
        for path in sorted(ROOT.rglob("*.py"))
        if not path.relative_to(ROOT).as_posix().startswith(EXCLUDED)
    ]


def test_product_code_mutates_evidence_only_through_listed_exceptions() -> None:
    found: set[tuple[str, str, str]] = set()
    for path in _product_sources():
        relative = path.relative_to(ROOT).as_posix()
        found |= violations(path.read_text(encoding="utf-8"), relative)
    assert found - ALLOWED == set(), "evidence mutation outside the §1.5 allowlist"
    # Every exception is still needed; a stale entry would hide a future one.
    assert ALLOWED - found == set()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('execute("UPDATE event_versions SET body = 1")', ("UPDATE", "event_versions")),
        (
            'text("DELETE FROM lifecycle_observations WHERE 1")',
            ("DELETE", "lifecycle_observations"),
        ),
        ('run("TRUNCATE TABLE object_versions, alerts")', ("TRUNCATE", "object_versions")),
        ('run(f"TRUNCATE {tables} RESTART IDENTITY")', ("TRUNCATE", "<dynamic>")),
        (
            "session.execute(update(ObjectVersionRow).values(uid=None))",
            ("UPDATE", "object_versions"),
        ),
        ("session.execute(delete(LogObservationRow.__table__))", ("DELETE", "log_observations")),
        ("session.execute(sa.delete(ChangeRecordRow))", ("DELETE", "change_records")),
    ],
)
def test_the_scan_catches_each_kind_of_mutation(source: str, expected: tuple[str, str]) -> None:
    assert ("fixture.py", *expected) in violations(source, "fixture.py")


def test_an_allowlisted_file_is_limited_to_its_listed_operations() -> None:
    offending = "session.execute(update(EventVersionRow).values(body={}))"
    found = violations(offending, "packages/storage/retention.py")
    assert found and not found <= ALLOWED


def test_the_scan_ignores_non_evidence_tables_and_plain_updates() -> None:
    harmless = """
payload.update(objects=1)
session.execute(update(EntityInstanceRow).values(name="x"))
session.execute(delete(IncidentRow))
log.info("UPDATE the dashboard later")
"""
    assert violations(harmless, "fixture.py") == set()


def test_evidence_table_list_matches_the_runtime_guard() -> None:
    guarded = {row.__tablename__ for row in AUTHORITATIVE_ROWS}
    transitional = {row.__tablename__ for row in TRANSITIONAL_MUTABLE_ROWS}
    assert guarded | transitional == set(EVIDENCE_TABLES)
    assert transitional == {"alerts"}  # until M19-3.6a


# --- runtime guard -----------------------------------------------------------

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    Base.metadata.create_all(engine)
    return Session(engine)


def _journal_row(session: Session) -> ObjectVersionRow:
    row = ObjectVersionRow(
        object_key="shop/Pod/web-0",
        namespace="shop",
        kind="Pod",
        name="web-0",
        uid="uid-a",
        observed_at=AT,
        content_hash="h",
        body={"kind": "Pod"},
    )
    session.add(row)
    session.commit()
    return row


def test_attribute_change_on_evidence_fails_at_flush(session: Session) -> None:
    row = _journal_row(session)
    row.body = {"kind": "Pod", "tampered": True}
    with pytest.raises(AuthoritativeEvidenceMutation, match="cannot be updated"):
        session.flush()
    session.rollback()
    assert session.scalar(select(ObjectVersionRow.body)) == {"kind": "Pod"}


def test_deleting_evidence_through_the_session_fails_at_flush(session: Session) -> None:
    session.add(
        LifecycleObservationRow(
            evidence_id="lifecycle:shop:Pod:uid-a:1",
            instance_uid="uid-a",
            namespace="shop",
            kind="Pod",
            name="web-0",
            type="OBSERVED",
            observed_at=AT,
            ingested_at=AT,
            source="collector",
            payload={},
        )
    )
    session.commit()
    row = session.scalar(select(LifecycleObservationRow))
    session.delete(row)
    with pytest.raises(AuthoritativeEvidenceMutation, match="cannot be deleted"):
        session.flush()


def test_non_evidence_and_transitional_rows_stay_mutable(session: Session) -> None:
    session.add(
        EntityInstanceRow(
            namespace="shop",
            kind="Pod",
            name="web-0",
            uid="uid-a",
            first_observed_at=AT,
            last_observed_at=AT,
        )
    )
    session.commit()
    instance = session.scalar(select(EntityInstanceRow))
    assert instance is not None
    instance.last_observed_at = AT.replace(hour=13)
    session.commit()
    # Alert ingestion still updates alerts in place until M19-3.6a.
    assert AlertRow in TRANSITIONAL_MUTABLE_ROWS
    assert not issubclass(IncidentRow, AUTHORITATIVE_ROWS)


def test_read_evidence_rows_and_core_statements_are_not_blocked(session: Session) -> None:
    row = _journal_row(session)
    assert row.uid == "uid-a"  # reading loads the row; nothing is written
    session.flush()
    # Migrations and retention use Core statements, outside the ORM guard.
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    with engine.begin() as connection:
        connection.execute(update(ObjectVersionRow).values(uid="uid-backfill"))
    assert session.scalar(select(ObjectVersionRow.uid).execution_options(populate_existing=True))
