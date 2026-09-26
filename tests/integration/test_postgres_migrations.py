"""Migrations on a real PostgreSQL (opt-in: make test-pg)."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.postgres


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migrations_reach_head_and_step_back_on_postgres(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = _config(postgres_url)
    head = ScriptDirectory.from_config(config).get_current_head()

    command.upgrade(config, "head")
    engine = create_engine(postgres_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == head
            )
        columns = {column["name"] for column in inspect(engine).get_columns("object_versions")}
        assert "uid" in columns

        command.downgrade(config, "-1")
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == head
            )
    finally:
        engine.dispose()
