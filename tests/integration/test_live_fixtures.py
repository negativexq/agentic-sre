"""Offline lifecycle and registry tests for the real benchmark harness."""

from datetime import UTC, datetime
from types import SimpleNamespace

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
from packages.evals.live_fixtures import (
    POD_CRASH_RESTARTS,
    POOL_PRESSURE_CONCURRENCY,
    POOL_PRESSURE_HOLD_MS,
    POOL_PRESSURE_WAVES,
    FixtureDefinition,
    LiveBenchmarkEnvironment,
    fixture_registry_is_complete,
)

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


def test_rollout_wait_requires_old_pods_to_be_gone() -> None:
    status = SimpleNamespace(updated_replicas=1, available_replicas=1, ready_replicas=1)
    current = SimpleNamespace(metadata=SimpleNamespace(deletion_timestamp=None))
    terminating_old = SimpleNamespace(metadata=SimpleNamespace(deletion_timestamp=NOW))

    assert not LiveBenchmarkEnvironment._rollout_is_complete(
        status, [current, terminating_old], desired_replicas=1
    )
    assert LiveBenchmarkEnvironment._rollout_is_complete(status, [current], desired_replicas=1)


def test_pool_pressure_fixture_has_bounded_concurrency() -> None:
    assert POOL_PRESSURE_CONCURRENCY == 18
    assert POOL_PRESSURE_HOLD_MS == 5_000
    assert POOL_PRESSURE_WAVES == 3
    assert POOL_PRESSURE_CONCURRENCY < 30
    assert POOL_PRESSURE_HOLD_MS <= 5_000


def test_payment_pod_crash_does_not_send_through_downtime_port_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()

    def fail_if_called(*_: object, **__: object) -> None:
        raise AssertionError("pod-crash stimulus must not send a payment request")

    monkeypatch.setattr(environment, "_payment_requests", fail_if_called)
    environment._payment_restart_baseline = 0
    environment._payment_process_start_baseline = 1.0
    monkeypatch.setattr(environment, "_restart_payment_container_repeatedly", lambda: None)
    environment.stimulate("payment_pod_crash")


def test_payment_pod_crash_prepare_only_captures_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()
    calls: list[str] = []
    monkeypatch.setattr(environment, "_payment_fault", lambda **_: calls.append("payment_fault"))
    monkeypatch.setattr(environment, "_order_fault", lambda **_: calls.append("order_fault"))
    monkeypatch.setattr(environment, "_restore_env", lambda _: calls.append("restore"))
    monkeypatch.setattr(environment, "_payment_restart_count", lambda: 4)
    monkeypatch.setattr(environment, "_payment_process_start_time", lambda: 100.0)
    monkeypatch.setattr(
        environment, "_restart_payment_container_once", lambda: calls.append("restart")
    )

    environment.prepare("payment_pod_crash")

    assert environment._payment_restart_baseline == 4
    assert environment._payment_process_start_baseline == 100.0
    assert "restart" not in calls


def test_payment_pod_crash_stimulus_confirms_two_restart_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()
    environment._payment_restart_baseline = 4
    environment._payment_process_start_baseline = 100.0
    state = {"restarts": 4, "starts": 100.0, "calls": 0}

    def restart() -> None:
        state["calls"] += 1
        state["restarts"] += 1
        state["starts"] += 1

    monkeypatch.setattr(environment, "_restart_payment_container_once", restart)
    monkeypatch.setattr(environment, "_payment_restart_count", lambda: int(state["restarts"]))
    monkeypatch.setattr(environment, "_payment_process_start_time", lambda: float(state["starts"]))
    monkeypatch.setattr(environment, "_wait_for_payment_health", lambda: None)

    environment.stimulate("payment_pod_crash")

    assert state["calls"] == POD_CRASH_RESTARTS == 2
    assert state["restarts"] == 6


def test_payment_pod_crash_uses_runtime_instability_alert() -> None:
    definition = FIXTURE_BY_NAME["payment_pod_crash"]

    assert definition.alert_name == "PaymentRuntimeInstability"
