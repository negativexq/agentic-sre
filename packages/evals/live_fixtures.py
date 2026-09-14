"""Real, sequential fault-to-incident fixtures for the live benchmark.

This module is benchmark setup authority.  Its mutation methods are deliberately
narrow and are never registered in the investigation tool registry.
"""

from __future__ import annotations

import importlib
import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from workload.common.contracts import OrderCreateRequest, PaymentRequest

from packages.contracts import Alert, AlertStatus, Incident, TimeWindow
from packages.evals.benchmark_store import BenchmarkPhase, PhaseLedger
from packages.evals.dataset import FROZEN_DATASET, FrozenIncident
from packages.investigation.registry import ReadOnlyToolRegistry
from packages.tools import BoundedToolExecutor
from packages.tools.contracts import ToolErrorCode, ToolFailure, ToolResponse

CONTROL_PLANE_DEFAULT = "http://localhost:18081"
ORDER_SERVICE_DEFAULT = "http://localhost:18000"
PAYMENT_SERVICE_DEFAULT = "http://localhost:18001"
PROMETHEUS_DEFAULT = "http://localhost:19090"
ALERTMANAGER_DEFAULT = "http://localhost:19093"
POOL_PRESSURE_CONCURRENCY = 18
POOL_PRESSURE_HOLD_MS = 3_500
POOL_PRESSURE_WAVES = 3
CONFIG_REGRESSION_CONCURRENCY = 20
CONFIG_REGRESSION_DELAY_MS = 10_000
A1G_CONFIG_REGRESSION_DELAY_MS = 3_000
POD_CRASH_RESTARTS = 2
# Kubernetes applies exponential restart backoff after consecutive crashes.  The
# fixture must allow that bounded recovery window instead of treating a delayed
# but valid restart as a missing fault.
POD_CRASH_RESTART_TIMEOUT_SECONDS = 90
POD_CRASH_HEALTH_TIMEOUT_SECONDS = 90
POD_CRASH_PROMETHEUS_TIMEOUT_SECONDS = 90
POD_CRASH_EXEC_TIMEOUT_SECONDS = 15
OBSERVABILITY_NAMESPACE = "observability"
OBSERVABILITY_RESET_DEPLOYMENTS = ("prometheus", "alertmanager")
ORDER_WORKER_LAG_ORDERS_PER_WAVE = 30
ORDER_WORKER_LAG_WAVES = 2
ORDER_WORKER_LAG_WAVE_INTERVAL_SECONDS = 5
ORDER_WORKER_LAG_PERSISTENCE_TARGET_SECONDS = 25
ORDER_WORKER_LAG_OBSERVATION_TIMEOUT_SECONDS = 60
EVIDENCE_PREFLIGHT_RETRIES = 5
EVIDENCE_PREFLIGHT_RETRY_INTERVAL_SECONDS = 1.0
PROMETHEUS_QUERY_RETRIES = 10
PROMETHEUS_QUERY_RETRY_INTERVAL_SECONDS = 1.0
WORKLOAD_TRANSPORT_RETRIES = 3
WORKLOAD_TRANSPORT_RETRY_INTERVAL_SECONDS = 0.5
WORKLOAD_REQUEST_TIMEOUT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class FixtureDefinition:
    """Safe setup metadata; no evaluator ground truth is stored here."""

    fixture: str
    alert_name: str
    service: str
    primary_tools: tuple[str, ...]


class BenchmarkStatePreparationResult(BaseModel):
    """Typed response from the local benchmark state preparation operation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    environment: str = Field(min_length=1, max_length=32)
    scope: str = Field(min_length=1, max_length=64)
    execution_id: str = Field(min_length=1, max_length=100)
    deleted_incidents: int = Field(ge=0)
    deleted_alerts: int = Field(ge=0)
    remaining_incidents: int = Field(ge=0)
    remaining_alerts: int = Field(ge=0)


class BenchmarkStateContaminatedError(RuntimeError):
    """Raised before investigation when local benchmark state is not isolated."""

    code = "A1_BENCHMARK_STATE_CONTAMINATED"


class FixtureStimulusError(RuntimeError):
    """Raised when a bounded fault stimulus did not reach its workload."""

    code = "FIXTURE_STIMULUS_FAILED"

    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details
        super().__init__(f"{self.code}: {json.dumps(details, sort_keys=True)}")


class WorkloadRequestError(RuntimeError):
    """Transport-level workload failure with bounded HTTP status context."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        self.http_status = http_status
        super().__init__(message)


class FixtureCorrelationError(RuntimeError):
    """Raised when alert/incident correlation fails with stage diagnostics."""

    def __init__(self, code: str, details: dict[str, Any]) -> None:
        self.code = code
        self.details = details
        super().__init__(f"{code}: {json.dumps(details, sort_keys=True)}")


FIXTURE_DEFINITIONS: tuple[FixtureDefinition, ...] = (
    FixtureDefinition(
        "payment_error_spike", "PaymentErrorRateHigh", "payment-service", ("service_error_rate",)
    ),
    FixtureDefinition(
        "order_error_spike", "OrderErrorRateHigh", "order-service", ("service_error_rate",)
    ),
    FixtureDefinition(
        "payment_dependency_latency",
        "OrderDependencyLatencyHigh",
        "order-service",
        ("service_latency", "slow_traces"),
    ),
    FixtureDefinition(
        "order_latency_spike", "OrderRequestLatencyHigh", "order-service", ("service_latency",)
    ),
    FixtureDefinition(
        "payment_db_pool_pressure",
        "PaymentDbAcquisitionSlow",
        "payment-service",
        ("db_connection_pressure",),
    ),
    FixtureDefinition(
        "order_db_query_latency", "OrderDbQueryLatencyHigh", "order-service", ("db_query_latency",)
    ),
    FixtureDefinition(
        "order_worker_lag", "OrderWorkerLagHigh", "order-worker", ("kafka_consumer_lag",)
    ),
    FixtureDefinition(
        "order_worker_failure", "OrderWorkerConsumerErrorsHigh", "order-worker", ("service_logs",)
    ),
    FixtureDefinition(
        "payment_pod_crash",
        "PaymentRuntimeInstability",
        "payment-service",
        ("kubernetes_container_restarts", "kubernetes_events"),
    ),
    FixtureDefinition(
        "payment_config_change",
        "PaymentServiceLatencyCritical",
        "payment-service",
        ("recent_configuration_changes",),
    ),
)

FIXTURE_BY_NAME = {item.fixture: item for item in FIXTURE_DEFINITIONS}

GENERALIZATION_FIXTURE_DEFINITIONS: tuple[FixtureDefinition, ...] = (
    FixtureDefinition(
        "a1g_order_payment_failure",
        "A1GOrderPaymentFailureHigh",
        "order-service",
        ("service_error_rate", "service_error_logs"),
    ),
    FixtureDefinition(
        "a1g_order_payment_db_pressure",
        "A1GOrderPaymentDbPressureHigh",
        "order-service",
        ("service_latency", "db_connection_pressure"),
    ),
    FixtureDefinition(
        "a1g_order_config_latency",
        "A1GOrderConfigLatencyHigh",
        "order-service",
        ("service_latency", "recent_configuration_changes"),
    ),
    FixtureDefinition(
        "a1g_order_payment_config_impact",
        "A1GOrderPaymentConfigImpactHigh",
        "order-service",
        ("service_latency", "recent_configuration_changes"),
    ),
    FixtureDefinition(
        "a1g_payment_db_query_latency",
        "A1GPaymentDbQueryLatencyHigh",
        "payment-service",
        ("service_latency", "db_query_latency"),
    ),
)

GENERALIZATION_FIXTURE_BY_NAME = {item.fixture: item for item in GENERALIZATION_FIXTURE_DEFINITIONS}


class FixtureStimulusObservation(BaseModel):
    """Bounded, evaluator-independent proof of controlled workload delivery."""

    model_config = ConfigDict(extra="forbid", strict=True)

    orders_attempted: int = Field(ge=0)
    orders_succeeded: int = Field(ge=0)
    orders_failed: int = Field(ge=0)
    expected_produced_metric_delta: int = Field(ge=0)
    observed_produced_metric_delta: float = Field(ge=0)
    observed_consumed_metric_delta: float = Field(ge=0)
    max_lag_proxy: float = Field(ge=0)
    seconds_above_lag_threshold: float = Field(ge=0)
    lag_threshold: float = Field(ge=0)
    prometheus_pending_observed: bool
    prometheus_firing_observed: bool
    alertmanager_firing_observed: bool


class WorkloadResult(BaseModel):
    """Bounded accounting for a controlled workload submission."""

    model_config = ConfigDict(extra="forbid", strict=True)

    attempted: int = Field(ge=0)
    successful: int = Field(ge=0)
    failed: int = Field(ge=0)
    started_at: datetime
    finished_at: datetime
    produced_before: float | None = Field(default=None, ge=0)
    produced_after: float | None = Field(default=None, ge=0)
    produced_delta: float | None = Field(default=None, ge=0)


class BaselineOracleResult(BaseModel):
    """Observable precondition result for a scenario trial."""

    model_config = ConfigDict(extra="forbid", strict=True)

    passed: bool
    workloads_healthy: bool
    prometheus_ready: bool
    alertmanager_ready: bool
    otel_ready: bool
    active_benchmark_alerts: list[str] = Field(max_length=100)
    canonical_incident_count: int = Field(ge=0)
    fault_env_clean: bool
    worker_rollout_stable: bool
    telemetry_baseline_valid: bool


class FaultOracleResult(BaseModel):
    """Explicit verification that a fixture mutation became active."""

    model_config = ConfigDict(extra="forbid", strict=True)

    passed: bool
    fixture: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=1, max_length=100)
    details: dict[str, Any] = Field(default_factory=dict, max_length=20)


class TriggerOracleResult(BaseModel):
    """Deterministic trigger condition result, separate from alert delivery."""

    model_config = ConfigDict(extra="forbid", strict=True)

    passed: bool
    fixture: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=1, max_length=100)
    details: dict[str, Any] = Field(default_factory=dict, max_length=20)


class BenchmarkTrial(BaseModel):
    """Safe lifecycle record for one real frozen scenario trial."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scenario_id: str = Field(min_length=1)
    fixture: str = Field(min_length=1)
    started_at: datetime
    fault_started_at: datetime | None = None
    alert_name: str = Field(min_length=1)
    alert_fingerprint: str | None = None
    incident_id: UUID | None = None
    investigation_started_at: datetime | None = None
    investigation_finished_at: datetime | None = None
    cleanup_started_at: datetime | None = None
    cleanup_finished_at: datetime | None = None
    alert_resolved: bool = False
    baseline_restored: bool = False
    setup_error: str | None = None
    stimulus: FixtureStimulusObservation | None = None
    workload: WorkloadResult | None = None
    fault_oracle: FaultOracleResult | None = None
    trigger_oracle: TriggerOracleResult | None = None


class FixtureEnvironment(Protocol):
    """Operations required by a benchmark fixture, separate from agent tools."""

    def baseline(self) -> None: ...

    def snapshot_incident_ids(self) -> set[str]: ...

    def prepare(self, fixture: str) -> None: ...

    def stimulate(self, fixture: str) -> None: ...

    def wait_for_incident(
        self, definition: FixtureDefinition, before_ids: set[str]
    ) -> tuple[Incident, tuple[Alert, ...]]: ...

    def cleanup(self, fixture: str) -> None: ...

    def verify_recovery(self, definition: FixtureDefinition, incident: Incident) -> bool: ...


class ControlPlaneClient:
    """Small bounded HTTP client for control-plane reads and journal writes."""

    def __init__(self, base_url: str = CONTROL_PLANE_DEFAULT) -> None:
        self.base_url = base_url.rstrip("/")

    def _request(self, path: str, *, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data else "GET",
        )
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read(2_000_001))
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise RuntimeError("control-plane request failed") from error

    def incidents(self) -> list[Incident]:
        payload = self._request("/api/v1/incidents")
        if not isinstance(payload, list):
            raise RuntimeError("control plane returned an invalid incident list")
        return [Incident.model_validate_json(json.dumps(item)) for item in payload]

    def alerts(self, incident_id: UUID) -> tuple[Alert, ...]:
        payload = self._request(f"/api/v1/incidents/{incident_id}/alerts")
        if not isinstance(payload, list):
            raise RuntimeError("control plane returned an invalid alert list")
        return tuple(Alert.model_validate_json(json.dumps(item)) for item in payload)

    def record_change(self, record: dict[str, Any]) -> None:
        self._request("/api/v1/changes", payload=record)

    def prepare_benchmark_state(self, execution_id: str) -> BenchmarkStatePreparationResult:
        """Request the explicitly authorized local benchmark state reset."""
        response = self._request(
            "/api/v1/benchmark/state/prepare",
            payload={
                "environment": "local-kind",
                "scope": "incident-alert-state",
                "confirmation": "reset-local-benchmark-state",
                "execution_id": execution_id,
            },
        )
        try:
            return BenchmarkStatePreparationResult.model_validate_json(json.dumps(response))
        except (TypeError, ValueError, ValidationError) as error:
            raise RuntimeError(
                "control plane returned an invalid benchmark-state response"
            ) from error

    def matching_incidents(
        self, definition: FixtureDefinition
    ) -> list[tuple[Incident, tuple[Alert, ...]]]:
        """Return incidents carrying the fixture's canonical alert identity."""
        matches: list[tuple[Incident, tuple[Alert, ...]]] = []
        for incident in self.incidents():
            alerts = self.alerts(incident.incident_id)
            matching = tuple(
                item
                for item in alerts
                if item.alert_name == definition.alert_name and item.service == definition.service
            )
            if matching:
                matches.append((incident, matching))
        return matches


class LiveBenchmarkEnvironment:
    """Real local-cluster environment used by fixture qualification and trials."""

    def __init__(
        self,
        *,
        control_plane_url: str = CONTROL_PLANE_DEFAULT,
        order_url: str = ORDER_SERVICE_DEFAULT,
        payment_url: str = PAYMENT_SERVICE_DEFAULT,
        prometheus_url: str = PROMETHEUS_DEFAULT,
        alertmanager_url: str = ALERTMANAGER_DEFAULT,
        namespace: str = "sre-demo",
    ) -> None:
        self.control_plane = ControlPlaneClient(control_plane_url)
        self.order_url = order_url.rstrip("/")
        self.payment_url = payment_url.rstrip("/")
        self.prometheus_url = prometheus_url.rstrip("/")
        self.alertmanager_url = alertmanager_url.rstrip("/")
        self.namespace = namespace
        self._original_env: dict[str, list[dict[str, str]]] = {}
        self._payment_restart_baseline: int | None = None
        self._payment_process_start_baseline: float | None = None
        self._payment_request_baseline: float | None = None
        self._payment_process_start_before_config_rollout: float | None = None
        self._worker_lag_produced_baseline: float | None = None
        self._worker_lag_consumed_baseline: float | None = None
        self._last_stimulus_observation: FixtureStimulusObservation | None = None
        self._last_baseline_oracle: BaselineOracleResult | None = None
        self._last_workload_result: WorkloadResult | None = None
        self._last_fault_oracle: FaultOracleResult | None = None
        self._last_trigger_oracle: TriggerOracleResult | None = None
        self._last_prepare_duration_seconds: float | None = None

    @property
    def last_stimulus_observation(self) -> FixtureStimulusObservation | None:
        """Return the latest bounded fixture stimulus proof for qualification."""
        return self._last_stimulus_observation

    @property
    def last_workload_result(self) -> WorkloadResult | None:
        return self._last_workload_result

    @property
    def last_baseline_oracle(self) -> BaselineOracleResult | None:
        return self._last_baseline_oracle

    @property
    def last_fault_oracle(self) -> FaultOracleResult | None:
        return self._last_fault_oracle

    @property
    def last_trigger_oracle(self) -> TriggerOracleResult | None:
        return self._last_trigger_oracle

    @property
    def last_prepare_duration_seconds(self) -> float | None:
        return self._last_prepare_duration_seconds

    @staticmethod
    def _post_json(
        url: str, value: BaseModel | dict[str, Any], *, timeout_seconds: float = 10
    ) -> Any:
        payload = (
            value.model_dump_json().encode()
            if isinstance(value, BaseModel)
            else json.dumps(value).encode()
        )
        request = Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read(1_000_001))
        except HTTPError as error:
            raise WorkloadRequestError(
                f"workload request returned HTTP {error.code}: {url.rsplit('/', 1)[-1]}",
                http_status=error.code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise WorkloadRequestError(
                f"workload request failed: {url.rsplit('/', 1)[-1]}"
            ) from error

    @staticmethod
    def _get_json(url: str) -> Any:
        try:
            with urlopen(url, timeout=10) as response:
                return json.loads(response.read(1_000_001))
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            raise RuntimeError("workload health request failed") from error

    def baseline(self) -> None:
        """Fail closed unless the two HTTP workloads and control plane are healthy."""
        result = self.baseline_oracle()
        if not result.passed:
            raise RuntimeError(f"baseline oracle failed: {result.model_dump_json()}")

    def baseline_oracle(self) -> BaselineOracleResult:
        """Check clean workload, control-plane, alert, and telemetry preconditions."""
        workloads_healthy = True
        try:
            self._wait_for_service_health_stable(self.order_url, "order workload")
            self._wait_for_service_health_stable(self.payment_url, "payment workload")
        except Exception:
            workloads_healthy = False
        canonical_incidents = self.control_plane.incidents()
        active_alerts: list[str] = []
        for incident in canonical_incidents:
            for alert in self.control_plane.alerts(incident.incident_id):
                if alert.status is AlertStatus.FIRING:
                    active_alerts.append(alert.alert_name)
        prometheus_ready = self._wait_for_endpoint_ready(f"{self.prometheus_url}/-/ready")
        alertmanager_ready = self._wait_for_endpoint_ready(f"{self.alertmanager_url}/-/ready")
        telemetry_baseline_valid = prometheus_ready and self._wait_for_prometheus_query()
        otel_ready = self._deployment_is_stable("otel-collector", namespace=OBSERVABILITY_NAMESPACE)
        fault_env_clean = self._fault_environment_is_clean()
        worker_rollout_stable = self._deployment_is_stable("order-worker")
        active_alerts.extend(self._active_alertmanager_alert_names())
        active_alerts = sorted(set(active_alerts))
        quiet = not active_alerts
        passed = all(
            (
                workloads_healthy,
                prometheus_ready,
                alertmanager_ready,
                otel_ready,
                telemetry_baseline_valid,
                fault_env_clean,
                worker_rollout_stable,
                quiet,
                not canonical_incidents,
            )
        )
        result = BaselineOracleResult(
            passed=passed,
            workloads_healthy=workloads_healthy,
            prometheus_ready=prometheus_ready,
            alertmanager_ready=alertmanager_ready,
            otel_ready=otel_ready,
            active_benchmark_alerts=active_alerts,
            canonical_incident_count=len(canonical_incidents),
            fault_env_clean=fault_env_clean,
            worker_rollout_stable=worker_rollout_stable,
            telemetry_baseline_valid=telemetry_baseline_valid,
        )
        self._last_baseline_oracle = result
        return result

    @staticmethod
    def _endpoint_ready(url: str) -> bool:
        try:
            with urlopen(url, timeout=5):
                return True
        except (HTTPError, URLError, TimeoutError, OSError):
            return False

    def _wait_for_endpoint_ready(self, url: str, timeout_seconds: float = 60) -> bool:
        """Allow a supervised port-forward to reconnect after observability reset."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._endpoint_ready(url):
                return True
            time.sleep(1)
        return False

    def _wait_for_prometheus_query(self, timeout_seconds: float = 60) -> bool:
        deadline = time.monotonic() + timeout_seconds
        consecutive_successes = 0
        while time.monotonic() < deadline:
            if self._prometheus_up_query_works():
                consecutive_successes += 1
                if consecutive_successes >= 3:
                    return True
            else:
                consecutive_successes = 0
            time.sleep(1)
        return False

    def _prometheus_up_query_works(self) -> bool:
        try:
            payload = self._get_json(
                f"{self.prometheus_url}/api/v1/query?{urlencode({'query': 'up'})}"
            )
        except RuntimeError:
            return False
        return payload.get("status") == "success" and isinstance(
            payload.get("data", {}).get("result"), list
        )

    def _prometheus_query_payload(self, query: str, error_message: str) -> Any:
        """Read a Prometheus instant query across a bounded port-forward gap."""
        url = f"{self.prometheus_url}/api/v1/query?{urlencode({'query': query})}"
        last_error: Exception | None = None
        for attempt in range(PROMETHEUS_QUERY_RETRIES):
            try:
                return self._get_json(url)
            except RuntimeError as error:
                last_error = error
                if attempt + 1 < PROMETHEUS_QUERY_RETRIES:
                    time.sleep(PROMETHEUS_QUERY_RETRY_INTERVAL_SECONDS)
        raise RuntimeError(error_message) from last_error

    def _wait_for_kafka_metric_series(self, timeout_seconds: float = 60) -> None:
        """Wait until the restarted Prometheus has scraped both Kafka series."""
        query = (
            'kafka_messages_total{topic="orders.created",direction=~"produced|consumed",'
            'service=~"order-service|order-worker"}'
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                payload = self._prometheus_query_payload(
                    query, "Prometheus Kafka metric series query failed"
                )
            except RuntimeError:
                time.sleep(1)
                continue
            results = payload.get("data", {}).get("result", []) if isinstance(payload, dict) else []
            if isinstance(results, list):
                services = {
                    item.get("metric", {}).get("service")
                    for item in results
                    if isinstance(item, dict)
                }
                directions = {
                    item.get("metric", {}).get("direction")
                    for item in results
                    if isinstance(item, dict)
                }
                if {"order-service", "order-worker"}.issubset(services) and {
                    "produced",
                    "consumed",
                }.issubset(directions):
                    return
            time.sleep(1)
        raise FixtureStimulusError(
            {"fixture": "order_worker_lag", "reason": "Kafka metric series unavailable"}
        )

    def _active_alertmanager_alert_names(self) -> list[str]:
        try:
            payload = self._get_json(f"{self.alertmanager_url}/api/v2/alerts")
        except RuntimeError:
            return ["ALERTMANAGER_UNAVAILABLE"]
        if not isinstance(payload, list):
            return ["ALERTMANAGER_INVALID_RESPONSE"]
        return sorted(
            {
                str(item.get("labels", {}).get("alertname"))
                for item in payload
                if isinstance(item, dict) and item.get("labels", {}).get("alertname")
            }
        )

    def _fault_environment_is_clean(self) -> bool:
        try:
            kubernetes = importlib.import_module("kubernetes")
            kubernetes_config = importlib.import_module("kubernetes.config")
            kubernetes_config.load_kube_config()
            api = kubernetes.client.AppsV1Api()
            for deployment in ("order-worker", "payment-service", "order-service"):
                current = api.read_namespaced_deployment(deployment, self.namespace)
                names = {
                    "order-worker": {"FAULT_WORKER_DELAY_MS", "FAULT_WORKER_FAILURE"},
                    "payment-service": {"FAULT_PAYMENT_DELAY_MS"},
                    "order-service": {"FAULT_ORDER_DELAY_MS"},
                }[deployment]
                if any(
                    item.name in names
                    for item in (current.spec.template.spec.containers[0].env or [])
                ):
                    return False
            return True
        except Exception:
            return False

    def _deployment_fault_values(self, deployment: str) -> dict[str, str]:
        kubernetes = importlib.import_module("kubernetes")
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_config.load_kube_config()
        api = kubernetes.client.AppsV1Api()
        current = api.read_namespaced_deployment(deployment, self.namespace)
        names = {
            "order-worker": {"FAULT_WORKER_DELAY_MS", "FAULT_WORKER_FAILURE"},
            "payment-service": {"FAULT_PAYMENT_DELAY_MS"},
            "order-service": {"FAULT_ORDER_DELAY_MS"},
        }[deployment]
        return {
            item.name: item.value
            for item in (current.spec.template.spec.containers[0].env or [])
            if item.name in names and item.value is not None
        }

    def verify_fault_activation(self, fixture: str) -> FaultOracleResult:
        """Verify fixture mutation at the runtime boundary before stimulation."""
        try:
            if fixture == "order_worker_lag":
                values = self._deployment_fault_values("order-worker")
                expected = {"FAULT_WORKER_DELAY_MS": "2000", "FAULT_WORKER_FAILURE": "false"}
                passed = values == expected and self._deployment_is_stable("order-worker")
                return FaultOracleResult(
                    passed=passed,
                    fixture=fixture,
                    code="FAULT_ACTIVE" if passed else "FAULT_NOT_ACTIVE",
                    details={"deployment": "order-worker", "values": values},
                )
            if fixture == "order_worker_failure":
                values = self._deployment_fault_values("order-worker")
                expected = {"FAULT_WORKER_DELAY_MS": "0", "FAULT_WORKER_FAILURE": "true"}
                passed = values == expected and self._deployment_is_stable("order-worker")
                return FaultOracleResult(
                    passed=passed,
                    fixture=fixture,
                    code="FAULT_ACTIVE" if passed else "FAULT_NOT_ACTIVE",
                    details={"deployment": "order-worker", "values": values},
                )
            return FaultOracleResult(
                passed=True,
                fixture=fixture,
                code="PREPARE_COMPLETED",
            )
        except Exception as error:
            return FaultOracleResult(
                passed=False,
                fixture=fixture,
                code="FAULT_ORACLE_ERROR",
                details={"error": type(error).__name__},
            )

    def verify_trigger(self, fixture: str) -> TriggerOracleResult:
        """Return the explicit deterministic trigger result for a fixture."""
        if self._last_trigger_oracle is not None and self._last_trigger_oracle.fixture == fixture:
            return self._last_trigger_oracle
        return TriggerOracleResult(
            passed=True,
            fixture=fixture,
            code="TRIGGER_VERIFIED_BY_ALERT_PATH",
        )

    def verify_workload(self, fixture: str) -> bool:
        """Require every workload-producing fixture to account for its delivery."""
        result = self._last_workload_result
        if result is None:
            # Pod-crash and worker-failure fixtures use the fault itself as the
            # stimulus. Their explicit fault oracle is the workload boundary.
            return fixture in {"payment_pod_crash", "order_worker_failure"}
        return result.successful == result.attempted and result.failed == 0

    def _deployment_is_stable(self, deployment: str, *, namespace: str | None = None) -> bool:
        try:
            kubernetes = importlib.import_module("kubernetes")
            kubernetes_config = importlib.import_module("kubernetes.config")
            kubernetes_config.load_kube_config()
            api = kubernetes.client.AppsV1Api()
            core = kubernetes.client.CoreV1Api()
            target_namespace = namespace or self.namespace
            current = api.read_namespaced_deployment(deployment, target_namespace)
            desired = current.spec.replicas or 1
            status = api.read_namespaced_deployment_status(deployment, target_namespace).status
            pods = core.list_namespaced_pod(
                target_namespace, label_selector=f"app={deployment}"
            ).items
            return self._rollout_is_complete(status, pods, desired)
        except Exception:
            return False

    def prepare_benchmark_state(self, execution_id: str) -> BenchmarkStatePreparationResult:
        """Reset guarded benchmark state and fail closed if it remains populated."""
        started = time.monotonic()
        try:
            self._reset_observability_state(execution_id)
            self._reset_workload_runtime_state(execution_id)
            prepared = self.control_plane.prepare_benchmark_state(execution_id)
            if prepared.remaining_incidents or prepared.remaining_alerts:
                raise BenchmarkStateContaminatedError(
                    f"{BenchmarkStateContaminatedError.code}: preparation left state populated"
                )
            remaining = self.control_plane.incidents()
            if remaining:
                details = ",".join(f"{item.incident_id}:{item.title}" for item in remaining[:8])
                raise BenchmarkStateContaminatedError(
                    f"{BenchmarkStateContaminatedError.code}: {details}"
                )
            return prepared
        finally:
            self._last_prepare_duration_seconds = max(time.monotonic() - started, 0.0)

    @staticmethod
    def _require_local_benchmark_context(kubernetes_config: Any) -> None:
        """Refuse observability mutation unless the active context is our local kind cluster."""
        contexts, active = kubernetes_config.list_kube_config_contexts()
        del contexts
        active_name = active.get("name") if isinstance(active, dict) else None
        if active_name != "kind-agentic-sre":
            raise BenchmarkStateContaminatedError(
                "A1_BENCHMARK_STATE_CONTAMINATED: unsafe Kubernetes context"
            )

    def _reset_observability_state(self, execution_id: str) -> None:
        """Recreate only ephemeral metric/alert stores in the guarded local cluster."""
        kubernetes = importlib.import_module("kubernetes")
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_config.load_kube_config()
        self._require_local_benchmark_context(kubernetes_config)
        api = kubernetes.client.AppsV1Api()
        deployments = api.list_namespaced_deployment(OBSERVABILITY_NAMESPACE).items
        names = {item.metadata.name for item in deployments}
        missing = sorted(set(OBSERVABILITY_RESET_DEPLOYMENTS) - names)
        if missing:
            raise BenchmarkStateContaminatedError(
                "A1_BENCHMARK_STATE_CONTAMINATED: missing observability deployment "
                + ",".join(missing)
            )
        marker = f"a1-r4-reset-{execution_id}-{time.monotonic_ns()}"
        for name in OBSERVABILITY_RESET_DEPLOYMENTS:
            api.patch_namespaced_deployment(
                name,
                OBSERVABILITY_NAMESPACE,
                {"spec": {"template": {"metadata": {"annotations": {"a1-r4/reset": marker}}}}},
            )
        for name in OBSERVABILITY_RESET_DEPLOYMENTS:
            self._wait_observability_rollout(api, name)

    def _reset_workload_runtime_state(self, execution_id: str) -> None:
        """Replace unhealthy/restarted payment pods before a new scenario."""
        kubernetes = importlib.import_module("kubernetes")
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_config.load_kube_config()
        self._require_local_benchmark_context(kubernetes_config)
        api = kubernetes.client.AppsV1Api()
        core = kubernetes.client.CoreV1Api()
        pods = core.list_namespaced_pod(self.namespace, label_selector="app=payment-service").items
        restart_count = sum(
            item.restart_count for pod in pods for item in (pod.status.container_statuses or [])
        )
        if restart_count == 0 and self._deployment_is_stable("payment-service"):
            return
        api.patch_namespaced_deployment(
            "payment-service",
            self.namespace,
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "a1-r4/workload-reset": f"{execution_id}-{time.monotonic_ns()}"
                            }
                        }
                    }
                }
            },
        )
        self._wait_rollout("payment-service")

    @staticmethod
    def _wait_observability_rollout(
        api: Any, deployment: str, timeout_seconds: float = 120
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            current = api.read_namespaced_deployment(deployment, OBSERVABILITY_NAMESPACE)
            status = current.status
            desired = current.spec.replicas or 1
            if (
                (status.updated_replicas or 0) == desired
                and (status.available_replicas or 0) == desired
                and (status.ready_replicas or 0) == desired
                and (status.unavailable_replicas or 0) == 0
            ):
                return
            time.sleep(2)
        raise TimeoutError(f"observability rollout timed out: {deployment}")

    def snapshot_incident_ids(self) -> set[str]:
        """Return the control-plane snapshot used for exact new-incident correlation."""
        return {str(item.incident_id) for item in self.control_plane.incidents()}

    def _set_fault(self, service_url: str, values: dict[str, Any]) -> None:
        self._post_json(f"{service_url}/__faults", values)

    def _payment_fault(self, **values: Any) -> None:
        self._set_fault(
            self.payment_url,
            {"delay_ms": 0, "error": False, "db_hold_ms": 0, "db_query_delay_ms": 0, **values},
        )

    def _order_fault(self, **values: Any) -> None:
        self._set_fault(
            self.order_url,
            {"delay_ms": 0, "error": False, "db_query_delay_ms": 0, **values},
        )

    def _kubectl_patch_env(self, deployment: str, values: dict[str, str]) -> None:
        """Patch only named benchmark env vars; never exposed to the agent registry."""
        kubernetes = importlib.import_module("kubernetes")
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_config.load_kube_config()
        api = kubernetes.client.AppsV1Api()
        current = api.read_namespaced_deployment(deployment, self.namespace)
        container = current.spec.template.spec.containers[0]
        existing = {
            item.name: item.value for item in (container.env or []) if item.value is not None
        }
        tracked_names = {
            "order-worker": {"FAULT_WORKER_DELAY_MS", "FAULT_WORKER_FAILURE"},
            "payment-service": {"FAULT_PAYMENT_DELAY_MS"},
            "order-service": {"FAULT_ORDER_DELAY_MS"},
        }.get(deployment, set(values))
        self._original_env[deployment] = [
            {"name": item.name, "value": item.value}
            for item in (container.env or [])
            if item.name in tracked_names
        ]
        merged = {**existing, **values}
        env = [{"name": name, "value": value} for name, value in sorted(merged.items())]
        api.patch_namespaced_deployment(
            deployment,
            self.namespace,
            {
                "spec": {
                    "template": {"spec": {"containers": [{"name": container.name, "env": env}]}}
                }
            },
        )
        self._wait_rollout(deployment)

    def _restore_env(self, deployment: str) -> None:
        kubernetes = importlib.import_module("kubernetes")
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_config.load_kube_config()
        api = kubernetes.client.AppsV1Api()
        current = api.read_namespaced_deployment(deployment, self.namespace)
        container = current.spec.template.spec.containers[0]
        names = {
            "order-worker": {"FAULT_WORKER_DELAY_MS", "FAULT_WORKER_FAILURE"},
            "payment-service": {"FAULT_PAYMENT_DELAY_MS"},
            "order-service": {"FAULT_ORDER_DELAY_MS"},
        }.get(deployment, set())
        remove_indexes = [
            index for index, item in enumerate(container.env or []) if item.name in names
        ]
        # A strategic-merge patch does not remove list entries that are absent
        # from the replacement list.  Use explicit JSON Patch removals so a
        # healthy baseline cannot inherit stale benchmark fault controls.
        if not remove_indexes:
            self._original_env.pop(deployment, None)
            return
        patch = [
            {"op": "remove", "path": f"/spec/template/spec/containers/0/env/{index}"}
            for index in reversed(remove_indexes)
        ]
        api.patch_namespaced_deployment(
            deployment,
            self.namespace,
            patch,
        )
        self._wait_rollout(deployment)
        self._original_env.pop(deployment, None)

    @staticmethod
    def _rollout_is_complete(status: Any, pods: list[Any], desired_replicas: int) -> bool:
        """Require the new replica set to be the only live pod set."""
        if (status.updated_replicas or 0) != desired_replicas:
            return False
        if (status.available_replicas or 0) != desired_replicas:
            return False
        if (status.ready_replicas or 0) != desired_replicas:
            return False
        return len(pods) == desired_replicas and all(
            pod.metadata.deletion_timestamp is None for pod in pods
        )

    def _wait_rollout(self, deployment: str, timeout_seconds: float = 120) -> None:
        kubernetes = importlib.import_module("kubernetes")
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_config.load_kube_config()
        api = kubernetes.client.AppsV1Api()
        core = kubernetes.client.CoreV1Api()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            deployment_obj = api.read_namespaced_deployment(deployment, self.namespace)
            desired_replicas = deployment_obj.spec.replicas or 1
            status = api.read_namespaced_deployment_status(deployment, self.namespace).status
            pods = core.list_namespaced_pod(
                self.namespace, label_selector=f"app={deployment}"
            ).items
            if self._rollout_is_complete(status, pods, desired_replicas):
                return
            time.sleep(2)
        raise TimeoutError(f"deployment rollout timed out: {deployment}")

    def _payment_requests(self, count: int = 20, interval_seconds: float = 0.0) -> int:
        succeeded = 0
        for _ in range(count):
            request = PaymentRequest(order_id=uuid4(), amount_cents=2_500, currency="USD")
            for attempt in range(WORKLOAD_TRANSPORT_RETRIES + 1):
                try:
                    self._post_json(
                        f"{self.payment_url}/payments",
                        request,
                        timeout_seconds=WORKLOAD_REQUEST_TIMEOUT_SECONDS,
                    )
                    succeeded += 1
                    break
                except WorkloadRequestError as error:
                    # A faulted HTTP workload is expected to return a bounded
                    # 4xx or 5xx response. The request was still delivered and
                    # must count toward workload accounting; transport failures
                    # receive only a bounded port-forward reconnect retry.
                    if error.http_status is not None:
                        succeeded += 1
                        break
                    if attempt < WORKLOAD_TRANSPORT_RETRIES:
                        time.sleep(WORKLOAD_TRANSPORT_RETRY_INTERVAL_SECONDS)
                except RuntimeError:
                    break
            if interval_seconds > 0:
                time.sleep(interval_seconds)
        return succeeded

    def _order_requests(self, count: int = 20, interval_seconds: float = 0.0) -> int:
        succeeded = 0
        for _ in range(count):
            request = OrderCreateRequest(
                customer_id=f"benchmark-{uuid4().hex[:12]}", amount_cents=2_500, currency="USD"
            )
            for attempt in range(WORKLOAD_TRANSPORT_RETRIES + 1):
                try:
                    self._post_json(
                        f"{self.order_url}/orders",
                        request,
                        timeout_seconds=WORKLOAD_REQUEST_TIMEOUT_SECONDS,
                    )
                    succeeded += 1
                    break
                except WorkloadRequestError as error:
                    if error.http_status is not None:
                        succeeded += 1
                        break
                    if attempt < WORKLOAD_TRANSPORT_RETRIES:
                        time.sleep(WORKLOAD_TRANSPORT_RETRY_INTERVAL_SECONDS)
                except RuntimeError:
                    break
            if interval_seconds > 0:
                time.sleep(interval_seconds)
        return succeeded

    def _concurrent_orders(self, count: int = 30) -> int:
        """Create a bounded burst so worker lag is observable before catch-up."""
        with ThreadPoolExecutor(max_workers=min(count, 30)) as executor:
            return sum(executor.map(lambda _: self._order_requests(1), range(count)))

    def _concurrent_payments(self, count: int = POOL_PRESSURE_CONCURRENCY) -> int:
        with ThreadPoolExecutor(max_workers=min(count, POOL_PRESSURE_CONCURRENCY)) as executor:
            return sum(executor.map(lambda _: self._payment_requests(1), range(count)))

    def _record_workload_result(
        self, attempted: int, successful: int, started_at: datetime
    ) -> None:
        self._last_workload_result = WorkloadResult(
            attempted=attempted,
            successful=successful,
            failed=attempted - successful,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    def _payment_restart_count(self) -> int:
        kubernetes = importlib.import_module("kubernetes")
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_config.load_kube_config()
        core = kubernetes.client.CoreV1Api()
        pods = core.list_namespaced_pod(self.namespace, label_selector="app=payment-service").items
        if not pods:
            raise RuntimeError("payment pod not found")
        return sum(
            item.restart_count for pod in pods for item in (pod.status.container_statuses or [])
        )

    def _payment_process_start_time(self) -> float | None:
        query = (
            'service_process_start_time_seconds{job="payment-service",service="payment-service"}'
        )
        request = Request(
            f"{self.prometheus_url}/api/v1/query?{urlencode({'query': query})}",
            method="GET",
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read(1_000_001))
        except (HTTPError, URLError, TimeoutError, OSError):
            # A supervised port-forward can briefly disappear while the
            # observability deployment is being reconciled. The caller's
            # bounded wait must observe this as "not sampled yet", not as an
            # unclassified fixture failure.
            return None
        results = payload.get("data", {}).get("result", [])
        if not isinstance(results, list) or not results:
            return None
        value = results[0].get("value")
        if not isinstance(value, list) or len(value) < 2:
            return None
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return None

    def _wait_for_payment_process_start(self) -> float:
        """Wait for Prometheus to scrape the payment process after a reset."""
        self._wait_until(
            lambda: self._payment_process_start_time() is not None,
            timeout_seconds=60,
            description="Prometheus process-start baseline unavailable",
        )
        value = self._payment_process_start_time()
        if value is None:  # pragma: no cover - protected by the wait predicate
            raise RuntimeError("Prometheus process-start baseline unavailable")
        return value

    def _payment_local_process_start_time(self) -> float | None:
        try:
            with urlopen(f"{self.payment_url}/metrics", timeout=10) as response:
                body = response.read(1_000_001).decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError):
            return None
        pattern = re.compile(
            r'^service_process_start_time_seconds\{service="payment-service"\}\s+([^\s]+)$'
        )
        for line in body.splitlines():
            match = pattern.match(line)
            if match is not None:
                try:
                    return float(match.group(1))
                except ValueError:
                    return None
        return None

    def _payment_request_count(self) -> float:
        query = (
            'sum(http_request_duration_seconds_count{job="payment-service",'
            'route="/payments",service="payment-service"})'
        )
        payload = self._prometheus_query_payload(query, "Prometheus payment request query failed")
        results = payload.get("data", {}).get("result", [])
        if not isinstance(results, list) or not results:
            return 0.0
        value = results[0].get("value")
        if not isinstance(value, list) or len(value) < 2:
            return 0.0
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return 0.0

    def _kafka_counter(self, service: str, direction: str) -> float:
        query = (
            "sum(kafka_messages_total{"
            f'service="{service}",topic="orders.created",direction="{direction}"'
            "})"
        )
        payload = self._prometheus_query_payload(query, "Prometheus Kafka counter query failed")
        results = payload.get("data", {}).get("result", [])
        if not isinstance(results, list) or not results:
            return 0.0
        value = results[0].get("value")
        if not isinstance(value, list) or len(value) < 2:
            return 0.0
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return 0.0

    def _kafka_increase(self, service: str, direction: str) -> float:
        query = (
            "sum(increase(kafka_messages_total{"
            f'service="{service}",topic="orders.created",direction="{direction}"'
            "}[2m]))"
        )
        payload = self._prometheus_query_payload(query, "Prometheus Kafka increase query failed")
        results = payload.get("data", {}).get("result", [])
        if not isinstance(results, list) or not results:
            return 0.0
        value = results[0].get("value")
        if not isinstance(value, list) or len(value) < 2:
            return 0.0
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return 0.0

    def _kafka_lag_proxy(self) -> float:
        produced = self._kafka_increase("order-service", "produced")
        consumed = self._kafka_increase("order-worker", "consumed")
        return max(produced - consumed, 0.0)

    @staticmethod
    def _wait_until(
        predicate: Callable[[], bool], *, timeout_seconds: float, description: str
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(1)
        raise TimeoutError(description)

    def _wait_for_service_health_stable(
        self, service_url: str, description: str, timeout_seconds: float = 60
    ) -> None:
        """Wait through port-forward reconnection and require three healthy reads."""
        deadline = time.monotonic() + timeout_seconds
        consecutive = 0
        while time.monotonic() < deadline:
            try:
                payload = self._get_json(f"{service_url}/health")
                healthy = isinstance(payload, dict) and payload.get("status") == "ok"
            except RuntimeError:
                healthy = False
            if healthy:
                consecutive += 1
                if consecutive == 3:
                    return
            else:
                consecutive = 0
            time.sleep(1)
        raise TimeoutError(f"{description} was not stable")

    def _wait_for_payment_health(self) -> None:
        def healthy() -> bool:
            try:
                payload = self._get_json(f"{self.payment_url}/health")
                return isinstance(payload, dict) and payload.get("status") == "ok"
            except (RuntimeError, OSError):
                return False

        self._wait_until(
            healthy,
            timeout_seconds=POD_CRASH_HEALTH_TIMEOUT_SECONDS,
            description="payment health did not recover after restart",
        )

    def _wait_for_payment_health_stable(self) -> None:
        """Confirm the post-rollout port-forward remains attached to a healthy pod."""
        deadline = time.monotonic() + POD_CRASH_HEALTH_TIMEOUT_SECONDS
        consecutive = 0
        while time.monotonic() < deadline:
            try:
                payload = self._get_json(f"{self.payment_url}/health")
                healthy = isinstance(payload, dict) and payload.get("status") == "ok"
            except (RuntimeError, OSError):
                healthy = False
            if healthy:
                consecutive += 1
                if consecutive == 3:
                    return
            else:
                consecutive = 0
            time.sleep(1)
        raise TimeoutError("payment health was not stable after deployment rollout")

    def _restart_payment_container_once(self) -> None:
        kubernetes = importlib.import_module("kubernetes")
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_stream = importlib.import_module("kubernetes.stream")
        kubernetes_config.load_kube_config()
        core = kubernetes.client.CoreV1Api()
        pods = core.list_namespaced_pod(self.namespace, label_selector="app=payment-service").items
        running_ready_pods = [
            pod
            for pod in pods
            if pod.status.phase == "Running"
            and pod.metadata.deletion_timestamp is None
            and any(status.ready for status in (pod.status.container_statuses or []))
        ]
        if len(running_ready_pods) != 1:
            raise RuntimeError("payment pod not found")
        pod = running_ready_pods[0]
        kubernetes_stream.stream(
            core.connect_get_namespaced_pod_exec,
            pod.metadata.name,
            self.namespace,
            command=["/bin/sh", "-c", "kill 1"],
            container=pod.spec.containers[0].name,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _request_timeout=POD_CRASH_EXEC_TIMEOUT_SECONDS,
        )

    def _restart_payment_container_repeatedly(self) -> None:
        previous_restart_count = self._payment_restart_baseline
        previous_process_start = self._payment_process_start_baseline
        if previous_restart_count is None or previous_process_start is None:
            raise RuntimeError("payment restart baseline was not captured")
        previous_process_start_value = previous_process_start
        for _ in range(POD_CRASH_RESTARTS):
            self._restart_payment_container_once()
            expected_restart_count = previous_restart_count + 1

            def restart_observed(expected: int = expected_restart_count) -> bool:
                return self._payment_restart_count() >= expected

            self._wait_until(
                restart_observed,
                timeout_seconds=POD_CRASH_RESTART_TIMEOUT_SECONDS,
                description="payment container restart was not observed",
            )
            self._wait_for_payment_health()

            def process_start_observed(previous: float = previous_process_start_value) -> bool:
                current = self._payment_process_start_time()
                return current is not None and current > previous

            self._wait_until(
                process_start_observed,
                timeout_seconds=POD_CRASH_PROMETHEUS_TIMEOUT_SECONDS,
                description="Prometheus did not observe the new payment process start",
            )
            previous_restart_count = expected_restart_count
            observed = self._payment_process_start_time()
            if observed is None:
                raise RuntimeError("Prometheus process-start observation disappeared")
            previous_process_start_value = observed

    def _record_configuration_change(
        self, deployment: str, before: dict[str, str], after: dict[str, str]
    ) -> None:
        now = datetime.now(UTC)
        self.control_plane.record_change(
            {
                "timestamp": now.isoformat(),
                "resource_type": "deployment",
                "resource_name": deployment,
                "change_type": "UPDATED",
                "scope": "CONFIGURATION",
                "before": before,
                "after": after,
                "revision": f"benchmark-{now.strftime('%Y%m%d%H%M%S%f')}",
                "source": "benchmark-harness",
            }
        )

    def _record_payment_config_change(self) -> None:
        self._record_configuration_change(
            "payment-service",
            {"FAULT_PAYMENT_DELAY_MS": "0"},
            {"FAULT_PAYMENT_DELAY_MS": str(CONFIG_REGRESSION_DELAY_MS)},
        )

    def prepare(self, fixture: str) -> None:
        self._last_stimulus_observation = None
        self._last_workload_result = None
        self._last_fault_oracle = None
        self._last_trigger_oracle = None
        self._payment_fault()
        self._order_fault()
        self._restore_env("order-worker")
        self._restore_env("payment-service")
        self._restore_env("order-service")
        if fixture == "payment_error_spike":
            self._payment_fault(error=True)
        elif fixture == "order_error_spike":
            self._order_fault(error=True)
        elif fixture == "payment_dependency_latency":
            self._payment_fault(delay_ms=700)
        elif fixture == "order_latency_spike":
            self._order_fault(delay_ms=700)
        elif fixture == "payment_db_pool_pressure":
            self._payment_fault(db_hold_ms=POOL_PRESSURE_HOLD_MS)
        elif fixture == "order_db_query_latency":
            self._order_fault(db_query_delay_ms=700)
        elif fixture == "order_worker_lag":
            self._kubectl_patch_env(
                "order-worker", {"FAULT_WORKER_DELAY_MS": "2000", "FAULT_WORKER_FAILURE": "false"}
            )
            self._wait_for_kafka_metric_series()
            self._worker_lag_produced_baseline = self._kafka_counter("order-service", "produced")
            self._worker_lag_consumed_baseline = self._kafka_counter("order-worker", "consumed")
            self._last_stimulus_observation = None
        elif fixture == "order_worker_failure":
            self._kubectl_patch_env(
                "order-worker", {"FAULT_WORKER_DELAY_MS": "0", "FAULT_WORKER_FAILURE": "true"}
            )
        elif fixture == "payment_pod_crash":
            self._payment_restart_baseline = self._payment_restart_count()
            self._payment_process_start_baseline = self._wait_for_payment_process_start()
        elif fixture == "payment_config_change":
            self._payment_process_start_before_config_rollout = (
                self._wait_for_payment_process_start()
            )
            self._kubectl_patch_env(
                "payment-service", {"FAULT_PAYMENT_DELAY_MS": str(CONFIG_REGRESSION_DELAY_MS)}
            )
            # A deployment rollout replaces the pod selected by the benchmark
            # port-forward.  The rollout can be complete before the forwarding
            # supervisor has attached to the new pod.  Establish the local
            # workload handshake before sending the stimulus so request
            # failures cannot be silently mistaken for missing telemetry.
            self._wait_for_payment_health_stable()
            previous_start = self._payment_process_start_before_config_rollout
            if previous_start is not None:
                self._wait_until(
                    lambda: (
                        (current := self._payment_process_start_time()) is not None
                        and current != previous_start
                    ),
                    timeout_seconds=30,
                    description="Prometheus did not observe the configuration rollout",
                )
                expected_start = self._payment_process_start_time()
                if expected_start is None:
                    raise RuntimeError("Prometheus configuration rollout value disappeared")
                self._wait_until(
                    lambda: self._payment_local_process_start_time() == expected_start,
                    timeout_seconds=30,
                    description="payment port-forward did not attach to the new pod",
                )
            self._payment_request_baseline = self._payment_request_count()
            self._record_payment_config_change()
        elif fixture == "a1g_order_payment_failure":
            self._payment_fault(error=True)
        elif fixture == "a1g_order_payment_db_pressure":
            self._payment_fault(db_hold_ms=POOL_PRESSURE_HOLD_MS)
        elif fixture == "a1g_order_config_latency":
            self._kubectl_patch_env("order-service", {"FAULT_ORDER_DELAY_MS": "700"})
            self._record_configuration_change(
                "order-service",
                {"FAULT_ORDER_DELAY_MS": "0"},
                {"FAULT_ORDER_DELAY_MS": "700"},
            )
        elif fixture == "a1g_order_payment_config_impact":
            self._payment_process_start_before_config_rollout = (
                self._wait_for_payment_process_start()
            )
            self._kubectl_patch_env(
                "payment-service", {"FAULT_PAYMENT_DELAY_MS": str(A1G_CONFIG_REGRESSION_DELAY_MS)}
            )
            self._wait_for_payment_health_stable()
            previous_start = self._payment_process_start_before_config_rollout
            if previous_start is not None:
                self._wait_until(
                    lambda: (
                        (current := self._payment_process_start_time()) is not None
                        and current != previous_start
                    ),
                    timeout_seconds=30,
                    description="Prometheus did not observe the configuration rollout",
                )
                expected_start = self._payment_process_start_time()
                if expected_start is None:
                    raise RuntimeError("Prometheus configuration rollout value disappeared")
                self._wait_until(
                    lambda: self._payment_local_process_start_time() == expected_start,
                    timeout_seconds=30,
                    description="payment port-forward did not attach to the new pod",
                )
            self._record_configuration_change(
                "payment-service",
                {"FAULT_PAYMENT_DELAY_MS": "0"},
                {"FAULT_PAYMENT_DELAY_MS": str(A1G_CONFIG_REGRESSION_DELAY_MS)},
            )
        elif fixture == "a1g_payment_db_query_latency":
            self._payment_fault(db_query_delay_ms=700)
        else:
            raise KeyError(fixture)
        self._last_fault_oracle = self.verify_fault_activation(fixture)
        if not self._last_fault_oracle.passed:
            raise FixtureStimulusError(self._last_fault_oracle.details)

    def stimulate(self, fixture: str) -> None:
        workload_started = datetime.now(UTC)
        if fixture in {
            "payment_error_spike",
            "payment_db_pool_pressure",
            "payment_pod_crash",
            "payment_config_change",
            "a1g_order_payment_failure",
            "a1g_order_payment_db_pressure",
            "a1g_order_payment_config_impact",
        }:
            if fixture == "payment_pod_crash":
                # The crash itself is the controlled stimulus.  Sending a request
                # through the same port-forward would turn expected downtime into
                # a harness transport failure and can tear down the forward.
                self._restart_payment_container_repeatedly()
                return
            if fixture == "payment_db_pool_pressure":
                attempted = POOL_PRESSURE_WAVES * POOL_PRESSURE_CONCURRENCY
                succeeded = 0
                for _ in range(POOL_PRESSURE_WAVES):
                    succeeded += self._concurrent_payments(count=POOL_PRESSURE_CONCURRENCY)
                self._record_workload_result(attempted, succeeded, workload_started)
            elif fixture == "a1g_order_payment_config_impact":
                attempted = 4 * 30
                succeeded = 0
                for _ in range(4):
                    succeeded += self._concurrent_orders(count=30)
                    time.sleep(5)
                self._record_workload_result(attempted, succeeded, workload_started)
            elif fixture == "payment_config_change":
                attempted = CONFIG_REGRESSION_CONCURRENCY
                succeeded = self._concurrent_payments(count=attempted)
                self._record_workload_result(attempted, succeeded, workload_started)
                baseline = self._payment_request_baseline
                if baseline is None:
                    raise RuntimeError("payment request baseline was not captured")
                self._wait_until(
                    lambda: (
                        self._payment_request_count() >= baseline + CONFIG_REGRESSION_CONCURRENCY
                    ),
                    timeout_seconds=30,
                    description="Prometheus did not observe all configuration stimulus requests",
                )
            elif fixture in {
                "a1g_order_payment_failure",
                "a1g_order_payment_db_pressure",
            }:
                if fixture == "a1g_order_payment_failure":
                    attempted = 60
                    succeeded = self._order_requests(count=attempted, interval_seconds=0.5)
                    self._record_workload_result(attempted, succeeded, workload_started)
                else:
                    attempted = 4 * 30
                    succeeded = 0
                    for _ in range(4):
                        succeeded += self._concurrent_orders(count=30)
                        time.sleep(5)
                    self._record_workload_result(attempted, succeeded, workload_started)
            else:
                attempted = 60
                succeeded = self._payment_requests(count=attempted, interval_seconds=0.5)
                self._record_workload_result(attempted, succeeded, workload_started)
        elif fixture in {
            "order_error_spike",
            "payment_dependency_latency",
            "order_latency_spike",
            "order_db_query_latency",
            "order_worker_lag",
            "order_worker_failure",
            "a1g_order_config_latency",
        }:
            if fixture == "order_worker_lag":
                self._stimulate_order_worker_lag()
                return
            if fixture == "a1g_order_config_latency":
                attempted = 4 * 20
                succeeded = 0
                for _ in range(4):
                    succeeded += self._order_requests(count=20, interval_seconds=0.25)
                    time.sleep(5)
                self._record_workload_result(attempted, succeeded, workload_started)
                return
            attempted = 60 if fixture in {"order_error_spike", "order_worker_failure"} else 30
            succeeded = self._order_requests(
                count=attempted,
                interval_seconds=0.5
                if fixture in {"order_error_spike", "order_worker_failure"}
                else 0.0,
            )
            self._record_workload_result(attempted, succeeded, workload_started)
        elif fixture == "a1g_payment_db_query_latency":
            # Keep the latency expression above threshold long enough for the
            # alert's `for` clause to transition from pending to firing while
            # retaining a bounded, repeatable stimulus.
            attempted = 4 * 60
            succeeded = 0
            for _ in range(4):
                succeeded += self._concurrent_payments(count=60)
                time.sleep(5)
            self._record_workload_result(attempted, succeeded, workload_started)
        else:
            raise KeyError(fixture)

    def _stimulate_order_worker_lag(self) -> None:
        """Deliver a bounded two-wave workload and prove the lag alert condition."""
        workload_started = datetime.now(UTC)
        expected = ORDER_WORKER_LAG_ORDERS_PER_WAVE * ORDER_WORKER_LAG_WAVES
        attempted = 0
        succeeded = 0
        for wave in range(ORDER_WORKER_LAG_WAVES):
            attempted += ORDER_WORKER_LAG_ORDERS_PER_WAVE
            wave_succeeded = self._concurrent_orders(ORDER_WORKER_LAG_ORDERS_PER_WAVE)
            succeeded += wave_succeeded
            if wave_succeeded != ORDER_WORKER_LAG_ORDERS_PER_WAVE:
                raise FixtureStimulusError(
                    {
                        "fixture": "order_worker_lag",
                        "orders_attempted": attempted,
                        "orders_succeeded": succeeded,
                        "orders_failed": attempted - succeeded,
                        "expected_wave_size": ORDER_WORKER_LAG_ORDERS_PER_WAVE,
                        "wave": wave + 1,
                    }
                )
            if wave + 1 < ORDER_WORKER_LAG_WAVES:
                time.sleep(ORDER_WORKER_LAG_WAVE_INTERVAL_SECONDS)
        if succeeded != expected:
            raise FixtureStimulusError(
                {
                    "fixture": "order_worker_lag",
                    "orders_attempted": attempted,
                    "orders_succeeded": succeeded,
                    "orders_failed": attempted - succeeded,
                    "expected_orders": expected,
                }
            )

        produced_baseline = self._worker_lag_produced_baseline
        consumed_baseline = self._worker_lag_consumed_baseline
        if produced_baseline is None or consumed_baseline is None:
            raise FixtureStimulusError(
                {"fixture": "order_worker_lag", "reason": "missing Kafka counter baseline"}
            )
        # The counters are sampled after stimulation so the observed delta is
        # an explicit delivery check, not an assumption based on HTTP attempts.
        self._wait_until(
            lambda: (
                self._kafka_counter("order-service", "produced") - produced_baseline >= expected
            ),
            timeout_seconds=60,
            description="Prometheus did not observe all order-worker lag stimulus events",
        )
        start = time.monotonic()
        previous = start
        above_seconds = 0.0
        max_lag = 0.0
        pending = False
        firing = False
        alertmanager_firing = False
        observed_produced = 0.0
        observed_consumed = 0.0
        deadline = start + ORDER_WORKER_LAG_OBSERVATION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            now = time.monotonic()
            lag = self._kafka_lag_proxy()
            max_lag = max(max_lag, lag)
            if lag > 10:
                above_seconds += max(now - previous, 0.0)
            previous = now
            observed_produced = max(
                self._kafka_counter("order-service", "produced") - produced_baseline, 0.0
            )
            observed_consumed = max(
                self._kafka_counter("order-worker", "consumed") - consumed_baseline, 0.0
            )
            state = self._prometheus_alert_state(
                FixtureDefinition("order_worker_lag", "OrderWorkerLagHigh", "order-worker", ())
            )
            pending = pending or state == "pending"
            firing = firing or state == "firing"
            alertmanager_state = self._alertmanager_alert_state(
                FixtureDefinition("order_worker_lag", "OrderWorkerLagHigh", "order-worker", ())
            )
            alertmanager_firing = alertmanager_firing or alertmanager_state == "firing"
            # Prometheus firing is necessary but not sufficient for the real
            # fixture path: Alertmanager must also have received the canonical
            # alert before this stimulus is considered qualified.  Do not exit
            # the observation window during Alertmanager's delivery lag.
            if (
                firing
                and alertmanager_firing
                and above_seconds >= ORDER_WORKER_LAG_PERSISTENCE_TARGET_SECONDS
            ):
                break
            time.sleep(0.5)
        self._last_stimulus_observation = FixtureStimulusObservation(
            orders_attempted=attempted,
            orders_succeeded=succeeded,
            orders_failed=attempted - succeeded,
            expected_produced_metric_delta=expected,
            observed_produced_metric_delta=observed_produced,
            observed_consumed_metric_delta=observed_consumed,
            max_lag_proxy=max_lag,
            seconds_above_lag_threshold=above_seconds,
            lag_threshold=10.0,
            prometheus_pending_observed=pending,
            prometheus_firing_observed=firing,
            alertmanager_firing_observed=alertmanager_firing,
        )
        workload_finished = datetime.now(UTC)
        self._last_workload_result = WorkloadResult(
            attempted=attempted,
            successful=succeeded,
            failed=attempted - succeeded,
            started_at=workload_started,
            finished_at=workload_finished,
            produced_before=produced_baseline,
            produced_after=produced_baseline + observed_produced,
            produced_delta=observed_produced,
        )
        trigger_passed = max_lag > 10 and above_seconds >= 15
        self._last_trigger_oracle = TriggerOracleResult(
            passed=trigger_passed,
            fixture="order_worker_lag",
            code="TRIGGER_VERIFIED" if trigger_passed else "TRIGGER_NOT_OBSERVED",
            details={
                "lag_peak": max_lag,
                "lag_threshold": 10.0,
                "seconds_above_threshold": above_seconds,
                "prometheus_firing_observed": firing,
            },
        )
        if not trigger_passed:
            raise FixtureStimulusError(
                {
                    "fixture": "order_worker_lag",
                    "orders_attempted": attempted,
                    "orders_succeeded": succeeded,
                    "observed_produced_metric_delta": observed_produced,
                    "observed_consumed_metric_delta": observed_consumed,
                    "max_lag_proxy": max_lag,
                    "seconds_above_lag_threshold": above_seconds,
                    "prometheus_pending_observed": pending,
                    "prometheus_firing_observed": firing,
                    "alertmanager_firing_observed": alertmanager_firing,
                }
            )

    def wait_for_incident(
        self,
        definition: FixtureDefinition,
        before_ids: set[str],
        *,
        timeout_seconds: float = 300,
        poll_seconds: float = 5,
    ) -> tuple[Incident, tuple[Alert, ...]]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for incident, matching in self.control_plane.matching_incidents(definition):
                if str(incident.incident_id) in before_ids:
                    continue
                if len(matching) == 1:
                    return incident, matching
                if len(matching) > 1:
                    raise RuntimeError("scenario alert matched multiple alerts")
            time.sleep(poll_seconds)
        matches = self.control_plane.matching_incidents(definition)
        stale = [
            {
                "incident_id": str(incident.incident_id),
                "alert_ids": [str(item.alert_id) for item in alerts],
                "fingerprints": sorted({item.fingerprint for item in alerts}),
                "statuses": sorted({item.status.value for item in alerts}),
                "starts_at": sorted({item.starts_at.isoformat() for item in alerts}),
            }
            for incident, alerts in matches
            if str(incident.incident_id) in before_ids
        ]
        diagnosis = {
            "expected_alert": definition.alert_name,
            "service": definition.service,
            "prometheus": self._prometheus_alert_state(definition),
            "alertmanager": self._alertmanager_alert_state(definition),
            "control_plane_matches": [
                {
                    "incident_id": str(incident.incident_id),
                    "alert_ids": [str(item.alert_id) for item in alerts],
                    "fingerprints": sorted({item.fingerprint for item in alerts}),
                }
                for incident, alerts in matches[:8]
            ],
            "stale_reused_incidents": stale[:8],
        }
        if stale:
            code = "STALE_REUSED_INCIDENT"
        elif diagnosis["prometheus"] == "absent":
            code = "PROMETHEUS_ALERT_NOT_OBSERVED"
        elif diagnosis["alertmanager"] == "absent":
            code = "ALERTMANAGER_ALERT_NOT_OBSERVED"
        elif not matches:
            code = "CONTROL_PLANE_INCIDENT_NOT_OBSERVED"
        else:
            code = "FRESH_INCIDENT_CORRELATION_FAILED"
        raise FixtureCorrelationError(code, diagnosis)

    def _prometheus_alert_state(self, definition: FixtureDefinition) -> str:
        """Probe the current Prometheus alert state for failure diagnostics only."""
        query = f'ALERTS{{alertname="{definition.alert_name}",service="{definition.service}"}}'
        try:
            payload = self._get_json(
                f"{self.prometheus_url}/api/v1/query?{urlencode({'query': query})}"
            )
        except RuntimeError:
            return "unavailable"
        results = payload.get("data", {}).get("result", [])
        states = {item.get("metric", {}).get("alertstate") for item in results}
        if "firing" in states:
            return "firing"
        if "pending" in states:
            return "pending"
        return "absent"

    def _alertmanager_alert_state(self, definition: FixtureDefinition) -> str:
        """Probe Alertmanager's active alert list for failure diagnostics only."""
        try:
            payload = self._get_json(f"{self.alertmanager_url}/api/v2/alerts")
        except RuntimeError:
            return "unavailable"
        if not isinstance(payload, list):
            return "unavailable"
        matches = [
            item
            for item in payload
            if isinstance(item, dict)
            and item.get("labels", {}).get("alertname") == definition.alert_name
            and item.get("labels", {}).get("service") == definition.service
        ]
        return "firing" if matches else "absent"

    def verify_recovery(self, definition: FixtureDefinition, incident: Incident) -> bool:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            alerts = self.control_plane.alerts(incident.incident_id)
            if any(
                item.alert_name == definition.alert_name and item.status.value == "RESOLVED"
                for item in alerts
            ):
                return self._wait_for_alerts_quiet()
            time.sleep(5)
        return False

    def _wait_for_alerts_quiet(self, timeout_seconds: float = 120) -> bool:
        """Require all alerts from prior trials to be resolved before the next trial."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            active = False
            for incident in self.control_plane.incidents():
                alerts = self.control_plane.alerts(incident.incident_id)
                if any(item.status is AlertStatus.FIRING for item in alerts):
                    active = True
                    break
            if not active:
                return True
            time.sleep(5)
        return False

    def cleanup(self, fixture: str) -> None:
        errors: list[Exception] = []
        if fixture == "payment_pod_crash":
            try:
                self._wait_for_payment_health_stable()
            except Exception as error:  # pragma: no cover - live environment
                errors.append(error)
        else:
            try:
                self._wait_for_service_health_stable(self.payment_url, "payment workload")
                self._payment_fault()
            except Exception as error:  # pragma: no cover - live environment
                errors.append(error)
        try:
            self._wait_for_service_health_stable(self.order_url, "order workload")
            self._order_fault()
        except Exception as error:  # pragma: no cover - live environment
            errors.append(error)
        try:
            self._restore_env("order-worker")
            self._restore_env("payment-service")
            self._restore_env("order-service")
        except Exception as error:  # pragma: no cover - live environment
            errors.append(error)
        if errors:
            raise RuntimeError("benchmark cleanup failed") from errors[0]


class FixtureLifecycle:
    """Sequential fixture runner with mandatory cleanup and correlation."""

    def __init__(
        self,
        environment: FixtureEnvironment,
        definitions: dict[str, FixtureDefinition] | None = None,
        phase_ledger: PhaseLedger | None = None,
    ) -> None:
        self.environment = environment
        self.definitions = definitions or FIXTURE_BY_NAME
        self.phase_ledger = phase_ledger

    def _phase(self, scenario_id: str, phase: BenchmarkPhase) -> Any:
        if self.phase_ledger is None:
            return nullcontext()
        return self.phase_ledger.phase(scenario_id, phase)

    def run(
        self,
        scenario: FrozenIncident,
        *,
        investigate: Callable[[Incident, tuple[Alert, ...]], Any] | None = None,
        snapshot_before_prepare: bool = False,
    ) -> tuple[BenchmarkTrial, Any | None]:
        definition = self.definitions[scenario.fixture]
        trial = BenchmarkTrial(
            scenario_id=scenario.scenario_id,
            fixture=scenario.fixture,
            started_at=datetime.now(UTC),
            alert_name=definition.alert_name,
        )
        result: Any | None = None
        incident: Incident | None = None
        before_ids: set[str] = set()
        try:
            with self._phase(scenario.scenario_id, BenchmarkPhase.VERIFY_BASELINE):
                self.environment.baseline()
            if snapshot_before_prepare:
                before_ids = self.environment.snapshot_incident_ids()
            with self._phase(scenario.scenario_id, BenchmarkPhase.PREPARE_ENVIRONMENT):
                with self._phase(scenario.scenario_id, BenchmarkPhase.INJECT_FAULT):
                    self.environment.prepare(scenario.fixture)
            if not snapshot_before_prepare:
                before_ids = self.environment.snapshot_incident_ids()
            trial = trial.model_copy(update={"fault_started_at": datetime.now(UTC)})
            fault_oracle = getattr(self.environment, "last_fault_oracle", None)
            if fault_oracle is not None:
                trial = trial.model_copy(update={"fault_oracle": fault_oracle})
            with self._phase(scenario.scenario_id, BenchmarkPhase.VERIFY_FAULT):
                verifier = getattr(self.environment, "verify_fault_activation", None)
                if callable(verifier):
                    verified = verifier(scenario.fixture)
                    trial = trial.model_copy(update={"fault_oracle": verified})
                    if not verified.passed:
                        raise FixtureStimulusError(verified.details)
            with self._phase(scenario.scenario_id, BenchmarkPhase.RUN_WORKLOAD):
                self.environment.stimulate(scenario.fixture)
            stimulus = getattr(self.environment, "last_stimulus_observation", None)
            if stimulus is not None:
                trial = trial.model_copy(update={"stimulus": stimulus})
            workload = getattr(self.environment, "last_workload_result", None)
            if workload is not None:
                trial = trial.model_copy(update={"workload": workload})
            trigger = getattr(self.environment, "last_trigger_oracle", None)
            if trigger is not None:
                trial = trial.model_copy(update={"trigger_oracle": trigger})
            with self._phase(scenario.scenario_id, BenchmarkPhase.VERIFY_WORKLOAD):
                verifier = getattr(self.environment, "verify_workload", None)
                if callable(verifier) and not verifier(scenario.fixture):
                    workload = getattr(self.environment, "last_workload_result", None)
                    details = workload.model_dump(mode="json") if workload is not None else {}
                    raise FixtureStimulusError({"fixture": scenario.fixture, "workload": details})
            with self._phase(scenario.scenario_id, BenchmarkPhase.VERIFY_TRIGGER):
                verifier = getattr(self.environment, "verify_trigger", None)
                if callable(verifier):
                    verified = verifier(scenario.fixture)
                    trial = trial.model_copy(update={"trigger_oracle": verified})
                    if not verified.passed:
                        raise FixtureStimulusError(verified.details)
            with self._phase(scenario.scenario_id, BenchmarkPhase.WAIT_FOR_ALERT):
                with self._phase(scenario.scenario_id, BenchmarkPhase.WAIT_FOR_INCIDENT):
                    incident, alerts = self.environment.wait_for_incident(definition, before_ids)
            trial = trial.model_copy(
                update={
                    "incident_id": incident.incident_id,
                    "alert_fingerprint": next(
                        item.fingerprint
                        for item in alerts
                        if item.alert_name == definition.alert_name
                    ),
                }
            )
            if investigate is not None:
                with self._phase(scenario.scenario_id, BenchmarkPhase.RUN_AGENT):
                    trial = trial.model_copy(update={"investigation_started_at": datetime.now(UTC)})
                    result = investigate(incident, alerts)
                    trial = trial.model_copy(
                        update={"investigation_finished_at": datetime.now(UTC)}
                    )
        except Exception as error:
            trial = trial.model_copy(update={"setup_error": type(error).__name__})
            raise
        finally:
            trial = trial.model_copy(update={"cleanup_started_at": datetime.now(UTC)})
            with self._phase(scenario.scenario_id, BenchmarkPhase.CLEANUP_FAULT):
                self.environment.cleanup(scenario.fixture)
            trial = trial.model_copy(update={"cleanup_finished_at": datetime.now(UTC)})
            if incident is not None:
                with self._phase(scenario.scenario_id, BenchmarkPhase.VERIFY_RECOVERY):
                    trial = trial.model_copy(
                        update={
                            "alert_resolved": self.environment.verify_recovery(
                                definition, incident
                            ),
                            "baseline_restored": True,
                        }
                    )
                with self._phase(scenario.scenario_id, BenchmarkPhase.RECONCILE_BASELINE):
                    trial = trial.model_copy(
                        update={"baseline_restored": self.baseline_reconcile()}
                    )
        return trial, result

    def baseline_reconcile(self) -> bool:
        """Re-run the environment baseline oracle between scenarios when available."""
        prepare = getattr(self.environment, "prepare_benchmark_state", None)
        if callable(prepare):
            prepare("scenario-reconciliation")
        oracle = getattr(self.environment, "baseline_oracle", None)
        if callable(oracle):
            return bool(oracle().passed)
        return True


def fixture_registry_is_complete() -> bool:
    """Prove the harness maps exactly the immutable dataset fixture names."""
    return {item.fixture for item in FROZEN_DATASET} == set(FIXTURE_BY_NAME) and len(
        FIXTURE_BY_NAME
    ) == 10


def select_harness_scenarios(requested: str | None = None) -> tuple[FrozenIncident, ...]:
    """Select validated development scenarios without changing the frozen dataset."""
    if requested is None or not requested.strip():
        return FROZEN_DATASET
    by_id = {item.scenario_id: item for item in FROZEN_DATASET}
    ids = tuple(item.strip() for item in requested.split(",") if item.strip())
    unknown = sorted(set(ids) - set(by_id))
    if unknown:
        raise ValueError(f"unknown harness scenarios: {','.join(unknown)}")
    if not ids:
        raise ValueError("harness scenario selection must contain at least one scenario ID")
    return tuple(by_id[item] for item in ids)


def preflight_evidence(
    registry: ReadOnlyToolRegistry,
    incident: Incident,
    alerts: tuple[Alert, ...],
    fixture: str,
    definitions: dict[str, FixtureDefinition] | None = None,
) -> tuple[ToolResponse, ...]:
    """Run the fixture's primary read-only evidence paths without a model call."""
    definition = (definitions or {**FIXTURE_BY_NAME, **GENERALIZATION_FIXTURE_BY_NAME})[fixture]
    relevant = next(item for item in alerts if item.alert_name == definition.alert_name)
    end = (
        relevant.ends_at
        if relevant.status is AlertStatus.RESOLVED and relevant.ends_at is not None
        else datetime.now(UTC)
    )
    window = TimeWindow(starts_at=relevant.starts_at, ends_at=end)
    args_by_tool: dict[str, dict[str, Any]] = {
        "service_error_rate": {"service": definition.service},
        "service_latency": {"service": definition.service},
        "db_connection_pressure": {"service": definition.service},
        "db_query_latency": {"service": definition.service},
        "kafka_consumer_lag": {"consumer": definition.service},
        "service_logs": {"service": definition.service, "range_seconds": None},
        "service_error_logs": {"service": definition.service, "pattern": "ERROR"},
        "slow_traces": {"service": definition.service},
        "kubernetes_container_restarts": {"deployment": definition.service},
        "kubernetes_events": {"deployment": definition.service},
        "recent_configuration_changes": {"deployment": definition.service},
        "recent_deployment_changes": {"deployment": definition.service},
    }
    executor = BoundedToolExecutor()
    results: list[ToolResponse] = []
    for name in definition.primary_tools:
        registered = registry.get(name)
        request = registered.request(
            incident.incident_id,
            args_by_tool[name],
            observation_window={
                "starts_at": window.starts_at.isoformat(),
                "ends_at": window.ends_at.isoformat(),
            },
        )
        for attempt in range(EVIDENCE_PREFLIGHT_RETRIES + 1):
            result = executor.execute(registered.tool, request)
            if isinstance(result, ToolResponse):
                results.append(result)
                break
            transient = isinstance(result, ToolFailure) and result.code in {
                ToolErrorCode.BACKEND_UNAVAILABLE,
                ToolErrorCode.BACKEND_TIMEOUT,
            }
            if not transient or attempt == EVIDENCE_PREFLIGHT_RETRIES:
                raise RuntimeError(
                    f"fixture evidence preflight failed: {name}:{getattr(result, 'code', 'unknown')}"
                )
            # Qualification runs use supervised local port-forwards. A pod
            # restart can create a short, expected reconnect gap; retry only
            # those bounded transport failures and never alter tool semantics.
            time.sleep(EVIDENCE_PREFLIGHT_RETRY_INTERVAL_SECONDS)
    return tuple(results)


__all__ = [
    "BenchmarkTrial",
    "ControlPlaneClient",
    "FIXTURE_BY_NAME",
    "FIXTURE_DEFINITIONS",
    "GENERALIZATION_FIXTURE_BY_NAME",
    "GENERALIZATION_FIXTURE_DEFINITIONS",
    "FixtureDefinition",
    "FixtureLifecycle",
    "LiveBenchmarkEnvironment",
    "fixture_registry_is_complete",
    "preflight_evidence",
    "select_harness_scenarios",
]
