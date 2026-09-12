"""Real, sequential fault-to-incident fixtures for the live benchmark.

This module is benchmark setup authority.  Its mutation methods are deliberately
narrow and are never registered in the investigation tool registry.
"""

from __future__ import annotations

import importlib
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from workload.common.contracts import OrderCreateRequest, PaymentRequest

from packages.contracts import Alert, AlertStatus, Incident, TimeWindow
from packages.evals.dataset import FROZEN_DATASET, FrozenIncident
from packages.investigation.registry import ReadOnlyToolRegistry
from packages.tools import BoundedToolExecutor
from packages.tools.contracts import ToolResponse

CONTROL_PLANE_DEFAULT = "http://localhost:18081"
ORDER_SERVICE_DEFAULT = "http://localhost:18000"
PAYMENT_SERVICE_DEFAULT = "http://localhost:18001"
POOL_PRESSURE_CONCURRENCY = 18
POOL_PRESSURE_HOLD_MS = 3_500


@dataclass(frozen=True, slots=True)
class FixtureDefinition:
    """Safe setup metadata; no evaluator ground truth is stored here."""

    fixture: str
    alert_name: str
    service: str
    primary_tools: tuple[str, ...]


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
        "PaymentServiceUnavailable",
        "payment-service",
        ("kubernetes_container_restarts", "kubernetes_events"),
    ),
    FixtureDefinition(
        "payment_config_change",
        "PaymentRequestLatencyCritical",
        "payment-service",
        ("recent_configuration_changes",),
    ),
)

FIXTURE_BY_NAME = {item.fixture: item for item in FIXTURE_DEFINITIONS}


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
        except (HTTPError, URLError, TimeoutError) as error:
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


class LiveBenchmarkEnvironment:
    """Real local-cluster environment used by fixture qualification and trials."""

    def __init__(
        self,
        *,
        control_plane_url: str = CONTROL_PLANE_DEFAULT,
        order_url: str = ORDER_SERVICE_DEFAULT,
        payment_url: str = PAYMENT_SERVICE_DEFAULT,
        namespace: str = "sre-demo",
    ) -> None:
        self.control_plane = ControlPlaneClient(control_plane_url)
        self.order_url = order_url.rstrip("/")
        self.payment_url = payment_url.rstrip("/")
        self.namespace = namespace
        self._original_env: dict[str, list[dict[str, str]]] = {}

    @staticmethod
    def _post_json(url: str, value: BaseModel | dict[str, Any]) -> Any:
        payload = (
            value.model_dump_json().encode()
            if isinstance(value, BaseModel)
            else json.dumps(value).encode()
        )
        request = Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read(1_000_001))
        except (HTTPError, URLError, TimeoutError) as error:
            raise RuntimeError(f"workload request failed: {url.rsplit('/', 1)[-1]}") from error

    @staticmethod
    def _get_json(url: str) -> Any:
        try:
            with urlopen(url, timeout=10) as response:
                return json.loads(response.read(1_000_001))
        except (HTTPError, URLError, TimeoutError) as error:
            raise RuntimeError("workload health request failed") from error

    def baseline(self) -> None:
        """Fail closed unless the two HTTP workloads and control plane are healthy."""
        for url in (f"{self.order_url}/health", f"{self.payment_url}/health"):
            payload = self._get_json(url)
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                raise RuntimeError("workload baseline is not healthy")
        self.control_plane.incidents()

    def snapshot_incident_ids(self) -> set[str]:
        """Return the control-plane snapshot used for exact new-incident correlation."""
        return {str(item.incident_id) for item in self.control_plane.incidents()}

    def _set_fault(self, service_url: str, values: dict[str, Any]) -> None:
        self._post_json(f"{service_url}/__faults", values)

    def _payment_fault(self, **values: Any) -> None:
        self._set_fault(
            self.payment_url, {"delay_ms": 0, "error": False, "db_hold_ms": 0, **values}
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
        if deployment not in self._original_env:
            return
        kubernetes = importlib.import_module("kubernetes")
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_config.load_kube_config()
        api = kubernetes.client.AppsV1Api()
        current = api.read_namespaced_deployment(deployment, self.namespace)
        container = current.spec.template.spec.containers[0]
        names = {
            "order-worker": {"FAULT_WORKER_DELAY_MS", "FAULT_WORKER_FAILURE"},
            "payment-service": {"FAULT_PAYMENT_DELAY_MS"},
        }.get(deployment, set())
        env = [
            {"name": item.name, "value": item.value}
            for item in (container.env or [])
            if item.name not in names and item.value is not None
        ]
        env.extend(self._original_env[deployment])
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

    def _payment_requests(self, count: int = 20, interval_seconds: float = 0.0) -> None:
        for _ in range(count):
            request = PaymentRequest(order_id=uuid4(), amount_cents=2_500, currency="USD")
            try:
                self._post_json(f"{self.payment_url}/payments", request)
            except RuntimeError:
                pass
            if interval_seconds > 0:
                time.sleep(interval_seconds)

    def _order_requests(self, count: int = 20, interval_seconds: float = 0.0) -> None:
        for _ in range(count):
            request = OrderCreateRequest(
                customer_id=f"benchmark-{uuid4().hex[:12]}", amount_cents=2_500, currency="USD"
            )
            try:
                self._post_json(f"{self.order_url}/orders", request)
            except RuntimeError:
                pass
            if interval_seconds > 0:
                time.sleep(interval_seconds)

    def _concurrent_orders(self, count: int = 30) -> None:
        """Create a bounded burst so worker lag is observable before catch-up."""
        with ThreadPoolExecutor(max_workers=min(count, 30)) as executor:
            list(executor.map(lambda _: self._order_requests(1), range(count)))

    def _concurrent_payments(self, count: int = POOL_PRESSURE_CONCURRENCY) -> None:
        with ThreadPoolExecutor(max_workers=min(count, POOL_PRESSURE_CONCURRENCY)) as executor:
            list(executor.map(lambda _: self._payment_requests(1), range(count)))

    def _restart_payment_container(self) -> None:
        kubernetes = importlib.import_module("kubernetes")
        kubernetes_config = importlib.import_module("kubernetes.config")
        kubernetes_stream = importlib.import_module("kubernetes.stream")
        kubernetes_config.load_kube_config()
        core = kubernetes.client.CoreV1Api()
        pods = core.list_namespaced_pod(self.namespace, label_selector="app=payment-service").items
        if not pods:
            raise RuntimeError("payment pod not found")
        kubernetes_stream.stream(
            core.connect_get_namespaced_pod_exec,
            pods[0].metadata.name,
            self.namespace,
            command=["/bin/sh", "-c", "kill 1"],
            container=pods[0].spec.containers[0].name,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )

    def _record_payment_config_change(self) -> None:
        now = datetime.now(UTC)
        self.control_plane.record_change(
            {
                "timestamp": now.isoformat(),
                "resource_type": "deployment",
                "resource_name": "payment-service",
                "change_type": "UPDATED",
                "scope": "CONFIGURATION",
                "before": {"FAULT_PAYMENT_DELAY_MS": "0"},
                "after": {"FAULT_PAYMENT_DELAY_MS": "3500"},
                "revision": f"benchmark-{now.strftime('%Y%m%d%H%M%S%f')}",
                "source": "benchmark-harness",
            }
        )

    def prepare(self, fixture: str) -> None:
        self._payment_fault()
        self._order_fault()
        self._restore_env("order-worker")
        self._restore_env("payment-service")
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
        elif fixture == "order_worker_failure":
            self._kubectl_patch_env(
                "order-worker", {"FAULT_WORKER_DELAY_MS": "0", "FAULT_WORKER_FAILURE": "true"}
            )
        elif fixture == "payment_pod_crash":
            self._restart_payment_container()
        elif fixture == "payment_config_change":
            self._kubectl_patch_env("payment-service", {"FAULT_PAYMENT_DELAY_MS": "3500"})
            self._record_payment_config_change()
        else:
            raise KeyError(fixture)

    def stimulate(self, fixture: str) -> None:
        if fixture in {
            "payment_error_spike",
            "payment_db_pool_pressure",
            "payment_pod_crash",
            "payment_config_change",
        }:
            if fixture == "payment_db_pool_pressure":
                self._concurrent_payments(count=POOL_PRESSURE_CONCURRENCY)
            else:
                self._payment_requests(count=60, interval_seconds=0.5)
        elif fixture in {
            "order_error_spike",
            "payment_dependency_latency",
            "order_latency_spike",
            "order_db_query_latency",
            "order_worker_lag",
            "order_worker_failure",
        }:
            if fixture == "order_worker_lag":
                self._concurrent_orders(count=30)
                return
            self._order_requests(
                count=60 if fixture in {"order_error_spike", "order_worker_failure"} else 30,
                interval_seconds=(
                    0.5 if fixture in {"order_error_spike", "order_worker_failure"} else 0.0
                ),
            )
        else:
            raise KeyError(fixture)

    def wait_for_incident(
        self,
        definition: FixtureDefinition,
        before_ids: set[str],
        *,
        timeout_seconds: float = 120,
        poll_seconds: float = 5,
    ) -> tuple[Incident, tuple[Alert, ...]]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for incident in self.control_plane.incidents():
                if str(incident.incident_id) in before_ids:
                    continue
                alerts = self.control_plane.alerts(incident.incident_id)
                matching = tuple(
                    item
                    for item in alerts
                    if item.alert_name == definition.alert_name
                    and item.service == definition.service
                )
                if len(matching) == 1:
                    return incident, alerts
                if len(matching) > 1:
                    raise RuntimeError("scenario alert matched multiple alerts")
            time.sleep(poll_seconds)
        raise TimeoutError(f"incident did not arrive: {definition.alert_name}")

    def verify_recovery(self, definition: FixtureDefinition, incident: Incident) -> bool:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            alerts = self.control_plane.alerts(incident.incident_id)
            if any(
                item.alert_name == definition.alert_name and item.status.value == "RESOLVED"
                for item in alerts
            ):
                return True
            time.sleep(5)
        return False

    def cleanup(self, fixture: str) -> None:
        errors: list[Exception] = []
        try:
            self._payment_fault()
        except Exception as error:  # pragma: no cover - live environment
            errors.append(error)
        try:
            self._order_fault()
        except Exception as error:  # pragma: no cover - live environment
            errors.append(error)
        try:
            self._restore_env("order-worker")
            self._restore_env("payment-service")
        except Exception as error:  # pragma: no cover - live environment
            errors.append(error)
        if errors:
            raise RuntimeError("benchmark cleanup failed") from errors[0]


class FixtureLifecycle:
    """Sequential fixture runner with mandatory cleanup and correlation."""

    def __init__(self, environment: FixtureEnvironment) -> None:
        self.environment = environment

    def run(
        self,
        scenario: FrozenIncident,
        *,
        investigate: Callable[[Incident, tuple[Alert, ...]], Any] | None = None,
    ) -> tuple[BenchmarkTrial, Any | None]:
        definition = FIXTURE_BY_NAME[scenario.fixture]
        trial = BenchmarkTrial(
            scenario_id=scenario.scenario_id,
            fixture=scenario.fixture,
            started_at=datetime.now(UTC),
            alert_name=definition.alert_name,
        )
        before_ids = self.environment.snapshot_incident_ids()
        result: Any | None = None
        incident: Incident | None = None
        try:
            self.environment.baseline()
            self.environment.prepare(scenario.fixture)
            trial = trial.model_copy(update={"fault_started_at": datetime.now(UTC)})
            self.environment.stimulate(scenario.fixture)
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
                trial = trial.model_copy(update={"investigation_started_at": datetime.now(UTC)})
                result = investigate(incident, alerts)
                trial = trial.model_copy(update={"investigation_finished_at": datetime.now(UTC)})
        except Exception as error:
            trial = trial.model_copy(update={"setup_error": type(error).__name__})
            raise
        finally:
            trial = trial.model_copy(update={"cleanup_started_at": datetime.now(UTC)})
            self.environment.cleanup(scenario.fixture)
            trial = trial.model_copy(update={"cleanup_finished_at": datetime.now(UTC)})
            if incident is not None:
                trial = trial.model_copy(
                    update={
                        "alert_resolved": self.environment.verify_recovery(definition, incident),
                        "baseline_restored": True,
                    }
                )
        return trial, result


def fixture_registry_is_complete() -> bool:
    """Prove the harness maps exactly the immutable dataset fixture names."""
    return {item.fixture for item in FROZEN_DATASET} == set(FIXTURE_BY_NAME) and len(
        FIXTURE_BY_NAME
    ) == 10


def preflight_evidence(
    registry: ReadOnlyToolRegistry,
    incident: Incident,
    alerts: tuple[Alert, ...],
    fixture: str,
) -> tuple[ToolResponse, ...]:
    """Run the fixture's primary read-only evidence paths without a model call."""
    definition = FIXTURE_BY_NAME[fixture]
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
        result = executor.execute(registered.tool, request)
        if not isinstance(result, ToolResponse):
            raise RuntimeError(
                f"fixture evidence preflight failed: {name}:{getattr(result, 'code', 'unknown')}"
            )
        results.append(result)
    return tuple(results)


__all__ = [
    "BenchmarkTrial",
    "ControlPlaneClient",
    "FIXTURE_BY_NAME",
    "FIXTURE_DEFINITIONS",
    "FixtureDefinition",
    "FixtureLifecycle",
    "LiveBenchmarkEnvironment",
    "fixture_registry_is_complete",
    "preflight_evidence",
]
