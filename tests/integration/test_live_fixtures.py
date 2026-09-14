"""Offline lifecycle and registry tests for the real benchmark harness."""

from datetime import UTC, datetime
from pathlib import Path
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
from packages.evals import (
    FIXTURE_BY_NAME,
    FIXTURE_DEFINITIONS,
    FROZEN_DATASET,
    FixtureLifecycle,
    PhaseLedger,
)
from packages.evals.benchmark_store import BenchmarkPhase
from packages.evals.live_fixtures import (
    ORDER_WORKER_LAG_ORDERS_PER_WAVE,
    ORDER_WORKER_LAG_PERSISTENCE_TARGET_SECONDS,
    ORDER_WORKER_LAG_WAVES,
    POD_CRASH_RESTARTS,
    POOL_PRESSURE_CONCURRENCY,
    POOL_PRESSURE_HOLD_MS,
    POOL_PRESSURE_WAVES,
    FixtureCorrelationError,
    FixtureDefinition,
    LiveBenchmarkEnvironment,
    WorkloadRequestError,
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


def test_fixture_alert_identities_are_unique() -> None:
    assert len({item.alert_name for item in FIXTURE_DEFINITIONS}) == len(FIXTURE_DEFINITIONS)


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


def test_fixture_lifecycle_emits_explicit_phase_records(tmp_path: Path) -> None:
    environment = FakeEnvironment()
    ledger = PhaseLedger(tmp_path / "phases.jsonl", execution_id="run-1")
    lifecycle = FixtureLifecycle(environment, phase_ledger=ledger)
    scenario = next(item for item in FROZEN_DATASET if item.fixture == "order_worker_lag")

    lifecycle.run(scenario)

    phases = {item.phase for item in ledger.read() if item.event == "start"}
    assert {
        BenchmarkPhase.VERIFY_BASELINE,
        BenchmarkPhase.PREPARE_ENVIRONMENT,
        BenchmarkPhase.INJECT_FAULT,
        BenchmarkPhase.VERIFY_FAULT,
        BenchmarkPhase.RUN_WORKLOAD,
        BenchmarkPhase.VERIFY_WORKLOAD,
        BenchmarkPhase.VERIFY_TRIGGER,
        BenchmarkPhase.WAIT_FOR_ALERT,
        BenchmarkPhase.WAIT_FOR_INCIDENT,
        BenchmarkPhase.CLEANUP_FAULT,
        BenchmarkPhase.VERIFY_RECOVERY,
        BenchmarkPhase.RECONCILE_BASELINE,
    }.issubset(phases)


def test_fixture_snapshot_is_taken_after_prepare_before_stimulus() -> None:
    environment = FakeEnvironment()
    lifecycle = FixtureLifecycle(environment)
    scenario = next(item for item in FROZEN_DATASET if item.fixture == "order_worker_lag")

    lifecycle.run(scenario)

    assert environment.calls.index("prepare:order_worker_lag") < environment.calls.index("snapshot")
    assert environment.calls.index("snapshot") < environment.calls.index(
        "stimulate:order_worker_lag"
    )


def test_rollout_wait_requires_old_pods_to_be_gone() -> None:
    status = SimpleNamespace(updated_replicas=1, available_replicas=1, ready_replicas=1)
    current = SimpleNamespace(metadata=SimpleNamespace(deletion_timestamp=None))
    terminating_old = SimpleNamespace(metadata=SimpleNamespace(deletion_timestamp=NOW))

    assert not LiveBenchmarkEnvironment._rollout_is_complete(
        status, [current, terminating_old], desired_replicas=1
    )
    assert LiveBenchmarkEnvironment._rollout_is_complete(status, [current], desired_replicas=1)


def test_baseline_oracle_checks_otel_in_observability_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()
    calls: list[tuple[str, str | None]] = []

    class FakeControlPlane:
        def incidents(self) -> list[Incident]:
            return []

        def alerts(self, _incident_id: object) -> tuple[Alert, ...]:
            return ()

    environment.control_plane = FakeControlPlane()  # type: ignore[assignment]
    monkeypatch.setattr(environment, "_wait_for_service_health_stable", lambda *_: None)
    monkeypatch.setattr(environment, "_wait_for_endpoint_ready", lambda *_: True)
    monkeypatch.setattr(environment, "_wait_for_prometheus_query", lambda *_: True)
    monkeypatch.setattr(environment, "_active_alertmanager_alert_names", lambda: [])
    monkeypatch.setattr(environment, "_fault_environment_is_clean", lambda: True)

    def stable(_deployment: str, *, namespace: str | None = None) -> bool:
        calls.append((_deployment, namespace))
        return True

    monkeypatch.setattr(
        environment,
        "_deployment_is_stable",
        stable,
    )

    result = environment.baseline_oracle()

    assert result.passed
    assert ("otel-collector", "observability") in calls


def test_pool_pressure_fixture_has_bounded_concurrency() -> None:
    assert POOL_PRESSURE_CONCURRENCY == 18
    assert POOL_PRESSURE_HOLD_MS == 3_500
    assert POOL_PRESSURE_WAVES == 3
    assert POOL_PRESSURE_CONCURRENCY < 30
    assert POOL_PRESSURE_HOLD_MS <= 5_000


def test_order_stimulus_reports_successes_instead_of_swallowing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()
    calls = 0

    def post(*_: object, **__: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated order transport failure")
        return {}

    monkeypatch.setattr(environment, "_post_json", post)

    assert environment._order_requests(count=3) == 2


def test_expected_http_fault_response_counts_as_delivered_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()

    def expected_fault_response(*_: object, **__: object) -> None:
        raise WorkloadRequestError("expected fault response", http_status=500)

    monkeypatch.setattr(environment, "_post_json", expected_fault_response)

    assert environment._order_requests(count=3) == 3


def test_workload_retries_transient_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()
    calls = 0

    def reconnecting_post(*_: object, **__: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkloadRequestError("port-forward reconnect")
        return {}

    monkeypatch.setattr(environment, "_post_json", reconnecting_post)
    monkeypatch.setattr("packages.evals.live_fixtures.time.sleep", lambda _: None)

    assert environment._order_requests(count=1) == 1
    assert calls == 2


def test_kafka_counter_retries_a_transient_prometheus_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()
    calls = 0

    def reconnecting_query(_: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("Prometheus port-forward reconnect")
        return {"data": {"result": [{"value": ["0", "42"]}]}}

    monkeypatch.setattr(environment, "_get_json", reconnecting_query)
    monkeypatch.setattr("packages.evals.live_fixtures.time.sleep", lambda _: None)

    assert environment._kafka_counter("order-service", "produced") == 42.0
    assert calls == 3


def test_worker_lag_waits_for_both_kafka_series_after_observability_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()
    monkeypatch.setattr(
        environment,
        "_prometheus_query_payload",
        lambda *_: {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"service": "order-service", "direction": "produced"}},
                    {"metric": {"service": "order-worker", "direction": "consumed"}},
                ]
            },
        },
    )

    environment._wait_for_kafka_metric_series(timeout_seconds=1)


def test_order_worker_lag_stimulus_is_bounded_with_persistence_margin() -> None:
    assert ORDER_WORKER_LAG_ORDERS_PER_WAVE == 30
    assert ORDER_WORKER_LAG_WAVES == 2
    assert ORDER_WORKER_LAG_ORDERS_PER_WAVE * ORDER_WORKER_LAG_WAVES == 60
    assert ORDER_WORKER_LAG_PERSISTENCE_TARGET_SECONDS >= 25


def test_workload_oracle_rejects_partial_delivery() -> None:
    environment = LiveBenchmarkEnvironment()
    environment._record_workload_result(3, 2, NOW)

    assert not environment.verify_workload("order_error_spike")


def test_workload_oracle_accepts_complete_delivery() -> None:
    environment = LiveBenchmarkEnvironment()
    environment._record_workload_result(3, 3, NOW)

    assert environment.verify_workload("order_error_spike")


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


def test_payment_health_poll_treats_port_forward_disconnect_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()
    attempts = {"count": 0}

    def health(_: str) -> dict[str, str]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("port-forward disconnected")
        return {"status": "ok"}

    monkeypatch.setattr(environment, "_get_json", health)

    environment._wait_for_payment_health()
    assert attempts["count"] == 2


def test_configuration_prepare_waits_for_post_rollout_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()
    calls: list[str] = []
    monkeypatch.setattr(environment, "_payment_fault", lambda **_: None)
    monkeypatch.setattr(environment, "_order_fault", lambda **_: None)
    monkeypatch.setattr(environment, "_restore_env", lambda _: None)
    monkeypatch.setattr(environment, "_kubectl_patch_env", lambda *_: calls.append("patch"))
    monkeypatch.setattr(
        environment, "_wait_for_payment_health_stable", lambda: calls.append("health")
    )
    monkeypatch.setattr(environment, "_wait_for_payment_process_start", lambda: None)
    monkeypatch.setattr(environment, "_payment_process_start_time", lambda: None)
    monkeypatch.setattr(environment, "_payment_request_count", lambda: 0.0)
    monkeypatch.setattr(
        environment, "_record_payment_config_change", lambda: calls.append("record")
    )

    environment.prepare("payment_config_change")

    assert calls == ["patch", "health", "record"]


def test_payment_pod_crash_uses_runtime_instability_alert() -> None:
    definition = FIXTURE_BY_NAME["payment_pod_crash"]

    assert definition.alert_name == "PaymentRuntimeInstability"


def test_recovery_waits_for_collateral_alerts_to_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = LiveBenchmarkEnvironment()
    calls = {"count": 0}
    collateral = Alert(
        alert_name="PaymentServiceLatencyCritical",
        service="payment-service",
        namespace="sre-demo",
        cluster="kind-agentic-sre",
        starts_at=NOW,
        ends_at=None,
        fingerprint="collateral-fingerprint",
        status=AlertStatus.FIRING,
        source=AlertSource.ALERTMANAGER,
    )

    class FakeControlPlane:
        def incidents(self) -> list[Incident]:
            return [_incident()]

        def alerts(self, _incident_id: object) -> tuple[Alert, ...]:
            calls["count"] += 1
            if calls["count"] == 1:
                return (collateral,)
            return (collateral.model_copy(update={"status": AlertStatus.RESOLVED}),)

    environment.control_plane = FakeControlPlane()  # type: ignore[assignment]
    monkeypatch.setattr("packages.evals.live_fixtures.time.sleep", lambda _: None)

    assert environment._wait_for_alerts_quiet(timeout_seconds=1)
    assert calls["count"] == 2


def test_wait_for_incident_classifies_reused_canonical_fingerprint_as_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing canonical incident is diagnostic, never a fresh trial result."""
    environment = LiveBenchmarkEnvironment()
    incident = _incident()
    alert = Alert(
        alert_name="PaymentErrorRateHigh",
        service="payment-service",
        namespace="sre-demo",
        cluster="kind-agentic-sre",
        starts_at=NOW,
        ends_at=None,
        fingerprint="canonical-payment-fingerprint",
        status=AlertStatus.FIRING,
        source=AlertSource.ALERTMANAGER,
    )

    class FakeControlPlane:
        def matching_incidents(
            self, _definition: FixtureDefinition
        ) -> list[tuple[Incident, tuple[Alert, ...]]]:
            return [(incident, (alert,))]

    environment.control_plane = FakeControlPlane()  # type: ignore[assignment]
    monkeypatch.setattr(environment, "_prometheus_alert_state", lambda _: "firing")
    monkeypatch.setattr(environment, "_alertmanager_alert_state", lambda _: "firing")

    with pytest.raises(FixtureCorrelationError) as raised:
        environment.wait_for_incident(
            FIXTURE_BY_NAME["payment_error_spike"],
            {str(incident.incident_id)},
            timeout_seconds=0,
            poll_seconds=0,
        )

    assert raised.value.code == "STALE_REUSED_INCIDENT"
    assert raised.value.details["stale_reused_incidents"][0]["fingerprints"] == [
        "canonical-payment-fingerprint"
    ]
