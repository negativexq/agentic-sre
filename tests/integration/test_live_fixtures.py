"""Offline lifecycle and registry tests for the real benchmark harness."""

from datetime import UTC, datetime

import pytest

from packages.contracts import (
    Alert,
    AlertSource,
    AlertStatus,
    Incident,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from packages.evals import FIXTURE_BY_NAME, FROZEN_DATASET, FixtureLifecycle
from packages.evals.live_fixtures import FixtureDefinition, fixture_registry_is_complete

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _incident() -> Incident:
    return Incident(
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        source=IncidentSource.ALERTMANAGER,
        title="OrderWorkerLagHigh",
        created_at=NOW,
        updated_at=NOW,
    )


class FakeEnvironment:
    def __init__(self, *, fail_investigation: bool = False) -> None:
        self.calls: list[str] = []
        self.incident = _incident()
        self.alert = Alert(
            alert_name="OrderWorkerLagHigh",
            service="order-worker",
            namespace="sre-demo",
            cluster="kind-agentic-sre",
            starts_at=NOW,
            ends_at=None,
            fingerprint="fixture-fingerprint",
            status=AlertStatus.FIRING,
            source=AlertSource.ALERTMANAGER,
        )
        self.fail_investigation = fail_investigation

    def baseline(self) -> None:
        self.calls.append("baseline")

    def snapshot_incident_ids(self) -> set[str]:
        self.calls.append("snapshot")
        return set()

    def prepare(self, fixture: str) -> None:
        self.calls.append(f"prepare:{fixture}")

    def stimulate(self, fixture: str) -> None:
        self.calls.append(f"stimulate:{fixture}")

    def wait_for_incident(
        self, definition: FixtureDefinition, before_ids: set[str]
    ) -> tuple[Incident, tuple[Alert, ...]]:
        self.calls.append(f"wait:{definition.fixture}")
        return self.incident, (self.alert,)

    def cleanup(self, fixture: str) -> None:
        self.calls.append(f"cleanup:{fixture}")

    def verify_recovery(self, definition: FixtureDefinition, incident: Incident) -> bool:
        self.calls.append("recovery")
        return True


def test_fixture_registry_matches_frozen_dataset_exactly() -> None:
    assert fixture_registry_is_complete()
    assert len(FROZEN_DATASET) == len(FIXTURE_BY_NAME) == 10
    assert {item.fixture for item in FROZEN_DATASET} == set(FIXTURE_BY_NAME)


def test_fixture_lifecycle_always_cleans_up_after_investigation_failure() -> None:
    environment = FakeEnvironment()
    lifecycle = FixtureLifecycle(environment)
    scenario = next(item for item in FROZEN_DATASET if item.fixture == "order_worker_lag")

    def investigate(_incident: Incident, _alerts: tuple[Alert, ...]) -> None:
        raise RuntimeError("investigation failed")

    with pytest.raises(RuntimeError, match="investigation failed"):
        lifecycle.run(scenario, investigate=investigate)
    assert "cleanup:order_worker_lag" in environment.calls
    assert environment.calls[-1] == "recovery"


def test_fixture_lifecycle_records_real_incident_identity_without_truth_fields() -> None:
    environment = FakeEnvironment()
    lifecycle = FixtureLifecycle(environment)
    scenario = next(item for item in FROZEN_DATASET if item.fixture == "order_worker_lag")

    trial, _ = lifecycle.run(scenario)

    assert trial.incident_id == environment.incident.incident_id
    assert trial.alert_name == "OrderWorkerLagHigh"
    assert scenario.mechanism.value not in trial.model_dump_json()
    assert scenario.suspected_trigger not in trial.model_dump_json()
