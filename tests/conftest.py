"""Opt-in PostgreSQL tests: marked ``postgres`` and skipped without TEST_POSTGRES_URL."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.getenv("TEST_POSTGRES_URL"):
        return
    skip = pytest.mark.skip(reason="TEST_POSTGRES_URL is not set; run make test-pg")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def postgres_url() -> Iterator[str]:
    """A fresh, empty database on the test server, dropped afterwards."""
    server = make_url(os.environ["TEST_POSTGRES_URL"])
    name = f"test_{uuid4().hex[:12]}"
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield server.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
