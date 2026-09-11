"""Three deterministic incident signal-class smoke scenarios."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from packages.e2e.smoke import DeterministicSmokeHarness
from packages.storage.models import Base


def test_three_signal_classes_complete_incident_smoke(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'smoke.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    harness = DeterministicSmokeHarness(factory)

    scenarios = (
        ("INC-SMOKE-001", "HighRequestLatency", "payment latency increase"),
        ("INC-SMOKE-002", "PodRestartSpike", "payment pod restart"),
        ("INC-SMOKE-003", "KafkaConsumerLag", "consumer lag increase"),
    )
    results = [harness.run(*scenario) for scenario in scenarios]

    assert [result.scenario for result in results] == [item[0] for item in scenarios]
    assert all(result.evidence_count == 4 for result in results)
    assert all(result.event_types[0] == "INCIDENT_CREATED" for result in results)
    assert all(result.event_types[-1] == "ALERT_RESOLVED" for result in results)
    engine.dispose()
