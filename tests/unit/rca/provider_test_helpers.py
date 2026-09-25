"""Small SQLite session factory for unit tests exercising ProviderAdapter writes."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from packages.storage.models import Base


def provider_session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)
