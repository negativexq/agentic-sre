"""Alerts that ended before the installation first observed the alert channel open no incident (contract §17)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from test_connector_streams import T0, delivery, firing, make, session_factory

from apps.control_plane.connector_intake import AlertStreamConsumer
from packages.rca.alert_coverage import AlertCoverageConfig
from packages.storage.models import IncidentRow

CONFIG = AlertCoverageConfig(poll_interval=timedelta(seconds=60), max_gap_polls=2)


def resolved(fingerprint: str, start: float, end: float) -> dict[str, Any]:
    return {
        **firing("OrderErrorRateHigh", fingerprint, T0 + timedelta(seconds=start)),
        "status": "resolved",
        "endsAt": (T0 + timedelta(seconds=end)).isoformat(),
    }


def incidents(factory: Any) -> int:
    with factory() as session:
        return int(session.query(IncidentRow).count())


def test_an_alert_that_ended_before_the_first_coverage_opens_nothing() -> None:
    connector, client, _, _, clock = make()
    clock.advance(400)
    connector.poll_alerts_once()  # W0 = T0 + 400 s
    connector.receive_webhook(delivery(resolved("f1", 0, 300)))  # a warm-up alert, delivered late
    factory = session_factory()
    consumer = AlertStreamConsumer(client, factory, config=CONFIG, clock=clock)
    consumer.step()
    assert incidents(factory) == 0 and consumer.ended_before_first_coverage == 1


def test_an_alert_that_ended_before_any_coverage_at_all_opens_nothing() -> None:
    connector, client, _, _, clock = make()
    clock.advance(400)
    connector.receive_webhook(delivery(resolved("f1", 0, 300)))
    factory = session_factory()
    AlertStreamConsumer(client, factory, config=CONFIG, clock=clock).step()
    assert incidents(factory) == 0


def test_an_alert_ending_after_the_first_coverage_or_still_firing_is_admitted() -> None:
    connector, client, _, _, clock = make()
    clock.advance(400)
    connector.poll_alerts_once()  # W0 = T0 + 400 s
    clock.advance(100)
    connector.receive_webhook(delivery(resolved("f1", 300, 450)))  # began before W0, ended after
    connector.receive_webhook(delivery({**firing("HighLatency", "f2"), "status": "firing"}))
    factory = session_factory()
    AlertStreamConsumer(client, factory, config=CONFIG, clock=clock).step()
    assert incidents(factory) == 2


def test_an_alert_inside_a_later_coverage_gap_is_admitted() -> None:
    connector, client, _, _, clock = make()
    connector.poll_alerts_once()  # W0 = T0
    factory = session_factory()
    consumer = AlertStreamConsumer(client, factory, config=CONFIG, clock=clock)
    consumer.step()
    clock.advance(900)  # the connector was away: a gap
    connector.poll_alerts_once()
    connector.receive_webhook(delivery(resolved("f1", 300, 600)))  # began and ended in the gap
    consumer.step()
    assert incidents(factory) == 1


def test_the_resolution_of_an_occurrence_already_recorded_always_passes() -> None:
    connector, client, _, _, clock = make()
    clock.advance(400)
    connector.receive_webhook(delivery({**firing("HighLatency", "f1"), "status": "firing"}))
    factory = session_factory()
    consumer = AlertStreamConsumer(client, factory, config=CONFIG, clock=clock)
    consumer.step()  # recorded before any coverage, while firing
    connector.poll_alerts_once()
    connector.receive_webhook(
        delivery({**resolved("f1", 0, 390), "labels": firing("HighLatency", "f1")["labels"]})
    )
    consumer.step()
    from packages.storage.models import AlertRow

    with factory() as session:
        (row,) = session.query(AlertRow).all()
        assert row.status == "RESOLVED" and incidents(factory) == 1
