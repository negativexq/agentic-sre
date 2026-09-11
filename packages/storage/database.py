"""SQLAlchemy engine and session boundaries."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create a synchronous engine for PostgreSQL or a test database."""
    return create_engine(database_url, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create an explicit session factory for application dependency injection."""
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield one transaction-scoped session and roll it back on failure."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
