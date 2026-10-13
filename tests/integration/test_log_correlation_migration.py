"""Add nullable assertions to an old log table without backfilling identities."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine


def check_migration(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    path = (
        Path(__file__).resolve().parents[2] / "alembic/versions/0033_log_correlation_attributes.py"
    )
    spec = spec_from_file_location("log_correlation_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        connection.execute(
            text(
                "create table log_observations (observation_id integer primary key, message text not null)"
            )
        )
        connection.execute(text("insert into log_observations values (1, 'old timeout')"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()  # fresh metadata may already contain the column
        assert connection.execute(
            text("select message, attributes from log_observations")
        ).one() == ("old timeout", None)
        migration.downgrade()
        assert "attributes" not in {
            c["name"] for c in inspect(connection).get_columns("log_observations")
        }
        assert (
            connection.execute(text("select message from log_observations")).scalar()
            == "old timeout"
        )


def test_additive_migration_preserves_legacy_rows_and_can_step_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    try:
        check_migration(engine, monkeypatch)
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_additive_log_correlation_migration_on_postgres(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(postgres_url)
    try:
        check_migration(engine, monkeypatch)
    finally:
        engine.dispose()
