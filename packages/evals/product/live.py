"""Live backend and ports of the product-resolution harness (M19-6.7).

Not imported by the package or by ``--list``: it is the only product module
that runs commands, talks HTTP or opens a database connection. Every command
is an argument list (no shell) against a dedicated Kind cluster, never the
developer's own cluster. The evidence port only reads.

Live execution is first accepted by the Kind smoke of M19-6.12; the stages
owned by later tasks raise until those tasks fill them.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import socket
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.evals.product import artifact, revisions
from packages.evals.product.actions import (
    IncidentRecord,
    JournalRecord,
    LifecycleRecord,
    PodIdentity,
)
from packages.evals.product.baseline import check_baseline
from packages.evals.product.revisions import (
    AlertView,
    IncidentView,
    RevisionScheduleError,
    RevisionView,
    ScheduleConfig,
    parse_time,
)
from packages.evals.product.runner import RunResult
from packages.evals.product.spec import ProductScenario
from packages.storage.models import (
    EvidenceRequirementRow,
    IncidentRow,
    LifecycleObservationRow,
    ObjectVersionRow,
)
from packages.storage.repositories import InvestigationReadRepository

logger = logging.getLogger(__name__)

CLUSTER = "agentic-sre-product"
NAMESPACE = "sre-demo"
IMAGES = ("control-plane", "migrator", "order-service", "payment-service", "order-worker")
WORKLOAD_DEPLOYMENTS = ("order-service", "payment-service", "order-worker")
DEPENDENCY_DEPLOYMENTS = ("postgres", "kafka", "redis")
OBSERVABILITY_DEPLOYMENTS = (
    "otel-collector",
    "prometheus",
    "kube-state-metrics",
    "loki",
    "tempo",
    "alertmanager",
    "grafana",
)
KAFKA_TOPICS = "/opt/kafka/bin/kafka-topics.sh"
# Harness traffic runs outside every watched/evidence namespace: it is not evidence.
TRAFFIC_NAMESPACE = "product-traffic"
TRAFFIC_POD = "product-traffic"
TRAFFIC_RATE = 2  # requests/s, one sequential worker (M19-6.12a)
TRAFFIC_SEED = 42
TRAFFIC_TARGET = f"http://order-service.{NAMESPACE}.svc.cluster.local:8000"


def traffic_manifest(duration: timedelta) -> list[dict[str, Any]]:
    """The in-cluster traffic Pod: the workload load generator against the order-service
    Service DNS, so traffic follows Service routing (readiness, selectors)."""
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": TRAFFIC_NAMESPACE}},
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": TRAFFIC_POD, "namespace": TRAFFIC_NAMESPACE},
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "load",
                        "image": "agentic-sre/order-service:dev",
                        "imagePullPolicy": "IfNotPresent",
                        "command": [
                            "python",
                            "-m",
                            "workload.load_generator",
                            "--base-url",
                            TRAFFIC_TARGET,
                            "--rate",
                            str(TRAFFIC_RATE),
                            "--duration",
                            str(int(duration.total_seconds())),
                            "--seed",
                            str(TRAFFIC_SEED),
                            "--concurrency",
                            "1",
                        ],  # fmt: skip
                        "resources": {
                            "requests": {"cpu": "20m", "memory": "64Mi"},
                            "limits": {"cpu": "200m", "memory": "256Mi"},
                        },
                    }
                ],
            },
        },
    ]


# Required on every product control plane: scheduler on, deterministic policy.
PRODUCT_CONTROL_PLANE_ENV = {
    "SRE_REEVALUATE": "true",
    "SRE_AUTO_DIAGNOSE": "true",
    "SRE_WATCH_INTERVAL_SECONDS": "15",
    "SRE_LLM_ENABLED": "false",
}

Run = Callable[[Sequence[str], Mapping[str, str], str | None], str]


def product_environment(base: Mapping[str, str]) -> dict[str, str]:
    """A child-process environment: no inherited ``SRE_LLM_*``, product values set."""
    env = {key: value for key, value in base.items() if not key.startswith("SRE_LLM_")}
    env.update(PRODUCT_CONTROL_PLANE_ENV)
    return env


def product_control_plane_manifest(documents: Sequence[Any]) -> list[Any]:
    """The control-plane manifest with the product environment pinned on its container.

    Explicit ``env`` entries win over ``envFrom`` in Kubernetes, so the pinned
    ``SRE_LLM_ENABLED=false`` holds whatever a ConfigMap carries; any other
    ``SRE_LLM_*`` entry is dropped.
    """
    rendered = copy.deepcopy([doc for doc in documents if doc])
    found = False
    for document in rendered:
        if document.get("kind") != "Deployment" or document["metadata"]["name"] != "control-plane":
            continue
        for container in document["spec"]["template"]["spec"]["containers"]:
            if container.get("name") != "control-plane":
                continue
            env = [
                item
                for item in container.get("env") or []
                if item["name"] not in PRODUCT_CONTROL_PLANE_ENV
                and not item["name"].startswith("SRE_LLM_")
            ]
            env += [
                {"name": name, "value": value} for name, value in PRODUCT_CONTROL_PLANE_ENV.items()
            ]
            container["env"] = env
            found = True
    if not found:
        raise ValueError("control-plane container not found in the manifest")
    return rendered


def baseline_faults(service: str) -> dict[str, Any]:
    """The full default fault config of a workload, from the workload's own model."""
    if service == "order-service":
        from workload.order_service.app import OrderFaultConfig  # noqa: PLC0415

        return OrderFaultConfig().model_dump(mode="json")
    if service == "payment-service":
        from workload.payment_service.app import FaultConfig  # noqa: PLC0415

        return FaultConfig().model_dump(mode="json")
    raise ValueError(f"{service} has no fault endpoint")


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _run(argv: Sequence[str], env: Mapping[str, str], stdin: str | None = None) -> str:
    try:
        return subprocess.run(
            list(argv), env=dict(env), input=stdin, check=True, capture_output=True, text=True
        ).stdout
    except subprocess.CalledProcessError as error:
        # A failed live step must say why: keep the tail of what the command printed.
        tail = ((error.stderr or "") + (error.stdout or ""))[-4000:]
        error.add_note(f"output tail:\n{tail}")
        raise


def _post_json(url: str, payload: Mapping[str, Any]) -> None:
    request = urllib.request.Request(  # noqa: S310 - fixed harness endpoints
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"{url} answered {response.status}")


def _request_json(
    url: str, payload: Mapping[str, Any], headers: Mapping[str, str]
) -> Mapping[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - fixed harness endpoints
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        document = json.loads(response.read().decode())
    if not isinstance(document, dict):
        raise RuntimeError(f"{url} did not answer a JSON object")
    return document


Http = Callable[[str, str, Mapping[str, str]], Any]


def _http(method: str, url: str, headers: Mapping[str, str]) -> Any:
    """A bodiless GET or POST answering JSON."""
    if method not in ("GET", "POST"):
        raise ValueError(f"unsupported method {method}")
    request = urllib.request.Request(  # noqa: S310 - fixed harness endpoints
        url, data=b"" if method == "POST" else None, headers=dict(headers), method=method
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        return json.loads(response.read().decode())


def _items(document: Any, what: str) -> list[Mapping[str, Any]]:
    if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
        raise RevisionScheduleError(f"{what} is not a list of objects")
    return document


@dataclass
class HttpControlPlaneReads:
    """``ControlPlaneReads`` over the control plane's GET API."""

    base_url: str
    http: Http = _http

    def _get(self, path: str) -> Any:
        return self.http("GET", f"{self.base_url}{path}", {})

    def incidents(self) -> Sequence[IncidentView]:
        return [
            IncidentView(str(item["incident_id"]), parse_time(item.get("created_at"), "created_at"))
            for item in _items(self._get("/api/v1/incidents"), "incidents")
        ]

    def alerts(self, incident_id: str) -> Sequence[AlertView]:
        alerts = _items(self._get(f"/api/v1/incidents/{incident_id}/alerts"), "alerts")
        for item in alerts:
            if not isinstance(item.get("labels"), dict):
                raise RevisionScheduleError("an incident alert has no labels")
        return [
            AlertView(dict(item["labels"]), parse_time(item.get("starts_at"), "starts_at"))
            for item in alerts
        ]

    def summaries(self, incident_id: str) -> list[Mapping[str, Any]]:
        """The persisted revision summaries, as the control plane lists them."""
        return _items(self._get(f"/api/v1/incidents/{incident_id}/diagnoses"), "diagnoses")

    def revisions(self, incident_id: str) -> Sequence[RevisionView]:
        return [
            RevisionView(
                int(item["diagnosis_id"]),
                int(item["revision_number"]),
                str(item["trigger"]),
                None if item.get("previous_diagnosis_id") is None
                else int(item["previous_diagnosis_id"]),
            )
            for item in self.summaries(incident_id)
        ]  # fmt: skip

    def onset(self, incident_id: str, revision_number: int) -> Any:
        detail = self._get(f"/api/v1/incidents/{incident_id}/diagnoses/{revision_number}")
        diagnosis = detail.get("diagnosis") if isinstance(detail, dict) else None
        symptoms = diagnosis.get("symptoms") if isinstance(diagnosis, dict) else None
        return symptoms.get("onset") if isinstance(symptoms, dict) else None


@dataclass
class HttpManualDiagnosis:
    """The one MANUAL diagnosis POST; a second call is refused."""

    base_url: str
    headers: Mapping[str, str]
    http: Http = _http
    _posted: bool = field(default=False, init=False)

    def post_manual(self, incident_id: str) -> None:
        if self._posted:
            raise RevisionScheduleError("a second MANUAL diagnosis POST")
        self._posted = True
        self.http(
            "POST",
            f"{self.base_url}/api/v1/incidents/{incident_id}/diagnosis?trigger=MANUAL",
            self.headers,
        )


@dataclass
class PrometheusActivations:
    """Prometheus ``/api/v1/alerts``: verification-only provenance, never RCA evidence."""

    base_url: str
    http: Http = _http

    def active_alerts(self) -> Sequence[Mapping[str, Any]]:
        document = self.http("GET", f"{self.base_url}/api/v1/alerts", {})
        data = document.get("data") if isinstance(document, dict) else None
        if not isinstance(document, dict) or document.get("status") != "success":
            raise RevisionScheduleError("Prometheus alerts did not answer success")
        alerts = data.get("alerts") if isinstance(data, dict) else None
        return _items(alerts, "Prometheus alerts")


@dataclass
class LiveClusterControl:
    """``ClusterControl`` over kubectl and the workload fault endpoints."""

    service_urls: Mapping[str, str]
    run: Run = _run
    env: Mapping[str, str] | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    post: Callable[[str, Mapping[str, Any]], None] = _post_json
    context: str = f"kind-{CLUSTER}"
    namespace: str = NAMESPACE
    replacement_timeout: timedelta = timedelta(minutes=3)

    def _kubectl(self, *args: str) -> str:
        argv = ["kubectl", "--context", self.context, "-n", self.namespace, *args]
        env = self.env if self.env is not None else product_environment(os.environ)
        return self.run(argv, env, None)

    def now(self) -> datetime:
        return self.clock()

    def wait(self, duration: timedelta) -> None:
        self.sleep(duration.total_seconds())

    def _pods(self, deployment: str) -> list[dict[str, Any]]:
        listing = json.loads(self._kubectl("get", "pods", "-l", f"app={deployment}", "-o", "json"))
        return [
            item
            for item in listing.get("items") or []
            if not (item.get("metadata") or {}).get("deletionTimestamp")
        ]

    def pod_of(self, deployment: str) -> PodIdentity:
        pods = self._pods(deployment)
        if len(pods) != 1:
            raise RuntimeError(f"{deployment}: expected one live Pod, found {len(pods)}")
        metadata = pods[0]["metadata"]
        return PodIdentity(str(metadata["name"]), str(metadata["uid"]))

    def set_not_ready(self, service: str, not_ready: bool) -> None:
        # /__faults replaces the whole config: always send the full baseline,
        # overriding readiness only.
        self.post(
            f"{self.service_urls[service]}/__faults",
            {**baseline_faults(service), "not_ready": not_ready},
        )

    def delete_pod(self, pod: PodIdentity) -> None:
        current = self._kubectl("get", "pod", pod.name, "-o", "jsonpath={.metadata.uid}").strip()
        if current != pod.uid:
            raise RuntimeError(f"{pod.name} is no longer uid {pod.uid}")
        self._kubectl("delete", "pod", pod.name, "--wait=false")

    def wait_for_replacement(self, deployment: str, old_uid: str) -> PodIdentity | None:
        deadline = self.now() + self.replacement_timeout
        while self.now() < deadline:
            for item in self._pods(deployment):
                statuses = (item.get("status") or {}).get("conditions") or []
                ready = any(
                    c.get("type") == "Ready" and c.get("status") == "True" for c in statuses
                )
                if item["metadata"]["uid"] != old_uid and ready:
                    return PodIdentity(str(item["metadata"]["name"]), str(item["metadata"]["uid"]))
            self.wait(timedelta(seconds=2))
        return None

    def patch_resources(
        self,
        deployment: str,
        container: str,
        limits: Mapping[str, str],
        requests: Mapping[str, str],
    ) -> None:
        argv = ["set", "resources", f"deployment/{deployment}", "-c", container]
        if limits:
            argv.append("--limits=" + ",".join(f"{key}={value}" for key, value in limits.items()))
        if requests:
            argv.append(
                "--requests=" + ",".join(f"{key}={value}" for key, value in requests.items())
            )
        self._kubectl(*argv)

    def wait_for_rollout(self, deployment: str) -> None:
        self._kubectl("rollout", "status", f"deployment/{deployment}", "--timeout=180s")

    def patch_service_selector(self, service: str, selector: Mapping[str, str]) -> None:
        # A JSON-patch replace sets exactly this selector; a merge patch would keep old keys.
        patch = [{"op": "replace", "path": "/spec/selector", "value": dict(selector)}]
        self._kubectl("patch", "service", service, "--type=json", "-p", json.dumps(patch))


@dataclass
class LiveEvidenceReader:
    """``EvidenceReader`` over the run's storage; it only selects."""

    _session_factory: sessionmaker[Session]
    namespace: str = NAMESPACE

    def lifecycle(self, instance_uid: str) -> Sequence[LifecycleRecord]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(LifecycleObservationRow)
                .where(
                    LifecycleObservationRow.namespace == self.namespace,
                    LifecycleObservationRow.instance_uid == instance_uid,
                )
                .order_by(
                    LifecycleObservationRow.observed_at, LifecycleObservationRow.observation_id
                )
            ).all()
            return [
                LifecycleRecord(
                    row.instance_uid, row.type, row.observed_at, row.source_at, row.payload
                )
                for row in rows
            ]

    def journal(self, kind: str, name: str | None = None) -> Sequence[JournalRecord]:
        query = select(ObjectVersionRow).where(
            ObjectVersionRow.namespace == self.namespace, ObjectVersionRow.kind == kind
        )
        if name is not None:
            query = query.where(ObjectVersionRow.name == name)
        with self._session_factory() as session:
            rows = session.scalars(
                query.order_by(ObjectVersionRow.observed_at, ObjectVersionRow.version_id)
            ).all()
            return [
                JournalRecord(row.kind, row.name, row.uid, row.observed_at, row.lifecycle, row.body)
                for row in rows
            ]

    def incidents(self) -> Sequence[IncidentRecord]:
        with self._session_factory() as session:
            rows = session.scalars(select(IncidentRow).order_by(IncidentRow.created_at)).all()
            return [IncidentRecord(str(row.incident_id), row.created_at) for row in rows]


@dataclass
class LiveArtifactReader:
    """Artifact facts from the run's storage (requirements, tape counts); it only selects."""

    _session_factory: sessionmaker[Session]

    def requirements(
        self, diagnosis_ids: Sequence[int]
    ) -> dict[int, list[artifact.RequirementEntry]]:
        """Requirements each revision opened, by ``diagnosis_id``."""
        with self._session_factory() as session:
            rows = session.scalars(
                select(EvidenceRequirementRow)
                .where(EvidenceRequirementRow.diagnosis_id.in_(list(diagnosis_ids)))
                .order_by(EvidenceRequirementRow.requirement_id)
            ).all()
            found: dict[int, list[artifact.RequirementEntry]] = {}
            for row in rows:
                found.setdefault(row.diagnosis_id, []).append(
                    artifact.RequirementEntry(
                        requirement_key=row.requirement_key,
                        rule_id=row.rule_id,
                        not_before=row.not_before,
                    )
                )
            return found

    def provider_tape(self, run_ids: Sequence[str]) -> artifact.ProviderTape:
        """Read counts over the runs' persisted tapes (the rows their tape digests cover)."""
        counts = {"CAPTURE": 0, "ENGINE": 0, "INVESTIGATION": 0}
        errors = 0
        with self._session_factory() as session:
            repository = InvestigationReadRepository(session)
            for run_id in run_ids:
                for row in repository.list_for_run(run_id):
                    counts[row.caller_class] += 1
                    errors += row.status == "ERROR"
        return artifact.ProviderTape(
            reads=sum(counts.values()),
            capture_reads=counts["CAPTURE"],
            engine_reads=counts["ENGINE"],
            investigation_reads=counts["INVESTIGATION"],
            errors=errors,
        )


class StageNotImplemented(RuntimeError):
    """A lifecycle stage whose behavior belongs to a later task."""


@dataclass
class LiveBackend:
    """Fresh-cluster lifecycle over kind, make, kubectl and port-forwards."""

    root: Path
    control_port: LiveClusterControl
    evidence_port: LiveEvidenceReader
    run: Run = _run
    spawn: Callable[[Sequence[str], Mapping[str, str]], Any] = lambda argv, env: subprocess.Popen(  # noqa: S603
        list(argv), env=dict(env), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    sleep: Callable[[float], None] = time.sleep
    cluster: str = CLUSTER
    namespace: str = NAMESPACE
    port_forwards: Mapping[str, int] | None = None
    control_plane_port: int | None = None
    postgres_port: int | None = None
    traffic_duration: timedelta = timedelta(hours=2)
    port_ready: Callable[[int], bool] = lambda port: _listening(port)
    before_teardown: Callable[[LiveBackend], None] | None = None
    control_plane_url: str | None = None
    api_token: str | None = None
    request_json: Callable[[str, Mapping[str, Any], Mapping[str, str]], Mapping[str, Any]] = (
        lambda url, payload, headers: _request_json(url, payload, headers)
    )
    prometheus_url: str | None = None
    prometheus_port: int | None = None
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    http: Http = _http
    run_id: str | None = None  # shared by one multi-scenario invocation; else per run start
    bench_root: Path | None = None
    git: artifact.GitProvenance | None = None
    artifact_reads: LiveArtifactReader | None = None

    def __post_init__(self) -> None:
        self._env = product_environment(os.environ)
        self._forwards: list[Any] = []
        self._collector_started_at: datetime | None = None
        self._t0: datetime | None = None
        self._r1: revisions.R1 | None = None
        self._r_early: revisions.REarly | None = None
        self._r2: int | None = None
        self._run: str | None = None
        self._traffic = False
        self.artifact_path: Path | None = None

    @property
    def _context(self) -> str:
        return f"kind-{self.cluster}"

    def _exec(self, *argv: str, stdin: str | None = None) -> str:
        return self.run(list(argv), self._env, stdin)

    def _kubectl(self, *args: str) -> str:
        return self._exec("kubectl", "--context", self._context, *args)

    def _apply(self, manifest: str) -> None:
        self._kubectl("apply", "-f", str(self.root / "infra" / "kubernetes" / manifest))

    def _rollout(self, deployment: str, namespace: str | None = None) -> None:
        self._kubectl(
            "-n",
            namespace or self.namespace,
            "rollout",
            "status",
            f"deployment/{deployment}",
            "--timeout=300s",
        )

    def cluster_up(self) -> None:
        self._run = self.run_id or artifact.run_id(self.control_port.now())
        self._exec(
            "kind", "create", "cluster", "--name", self.cluster,
            "--config", str(self.root / "infra" / "kubernetes" / "kind-config.yaml"),
        )  # fmt: skip

    def deploy_observability_and_workload(self) -> None:
        self._exec("make", "-C", str(self.root), "images")
        for image in IMAGES:
            self._exec(
                "kind", "load", "docker-image", f"agentic-sre/{image}:dev", "--name", self.cluster
            )
        # The order of ``make deploy``: the workload ConfigMap exists before the
        # migration job needs it; the workload restarts once the schema exists.
        for manifest in (
            "namespace.yaml",
            "observability.yaml",
            "tools-rbac.yaml",
            "workload.yaml",
            "dependencies.yaml",
        ):
            self._apply(manifest)
        for deployment in DEPENDENCY_DEPLOYMENTS:
            self._rollout(deployment)
        self._create_topic("orders.created")
        self._kubectl("-n", self.namespace, "delete", "job", "db-migration", "--ignore-not-found")
        self._apply("db-migration.yaml")
        self._kubectl(
            "-n",
            self.namespace,
            "wait",
            "--for=condition=complete",
            "job/db-migration",
            "--timeout=180s",
        )
        self._kubectl(
            "-n", self.namespace, "rollout", "restart",
            *(f"deployment/{name}" for name in WORKLOAD_DEPLOYMENTS),
        )  # fmt: skip

    def _create_topic(self, topic: str) -> None:
        kafka = ("-n", self.namespace, "exec", "deployment/kafka", "--", KAFKA_TOPICS)
        for _ in range(60):
            try:
                self._kubectl(*kafka, "--list", "--bootstrap-server", "localhost:9092")
                break
            except subprocess.CalledProcessError:
                self.sleep(2)
        else:
            raise RuntimeError("Kafka never answered a topic listing")
        self._kubectl(
            *kafka, "--create", "--if-not-exists", "--topic", topic,
            "--bootstrap-server", "localhost:9092",
        )  # fmt: skip

    def wait_workload_ready(self) -> None:
        for deployment in WORKLOAD_DEPLOYMENTS:
            self._rollout(deployment)
        for deployment in OBSERVABILITY_DEPLOYMENTS:
            self._rollout(deployment, "observability")

    def start_fresh_db_and_control_plane(self) -> None:
        # The cluster's Postgres has no persistent volume: storage is fresh
        # because the cluster is. The collector starts after the workload.
        # No evidence exists before the collector starts: the probe window opens here.
        self._collector_started_at = self.control_port.now()
        path = self.root / "infra" / "kubernetes" / "control-plane.yaml"
        rendered = product_control_plane_manifest(list(yaml.safe_load_all(path.read_text())))
        self._exec(
            "kubectl", "--context", self._context, "apply", "-f", "-",
            stdin=yaml.safe_dump_all(rendered),
        )  # fmt: skip
        self._rollout("control-plane")
        for service, port in (self.port_forwards or {}).items():
            self._forwards.append(
                self.spawn(
                    ["kubectl", "--context", self._context, "-n", self.namespace,
                     "port-forward", f"service/{service}", f"{port}:8000"],
                    self._env,
                )
            )  # fmt: skip
        for namespace, service, local, remote in (
            (self.namespace, "control-plane", self.control_plane_port, 8000),
            (self.namespace, "postgres", self.postgres_port, 5432),
            ("observability", "prometheus", self.prometheus_port, 9090),
        ):
            if local is None:
                continue
            self._forwards.append(
                self.spawn(
                    ["kubectl", "--context", self._context, "-n", namespace,
                     "port-forward", f"service/{service}", f"{local}:{remote}"],
                    self._env,
                )
            )  # fmt: skip
        self._await_forwards()

    def _await_forwards(self) -> None:
        ports = [
            *(self.port_forwards or {}).values(),
            *(p for p in (self.control_plane_port, self.postgres_port, self.prometheus_port) if p),
        ]
        for port in ports:
            for _ in range(60):
                if self.port_ready(port):
                    break
                self.sleep(0.5)
            else:
                raise RuntimeError(f"port-forward on {port} never listened")

    def warmup(self, duration: timedelta) -> None:
        self.sleep(duration.total_seconds())

    def clean_baseline(self, scenario: ProductScenario) -> None:
        if self._collector_started_at is None:
            raise RuntimeError("the control plane has not been started")
        reference_at = self.control_port.now()
        document = self.request_json(
            f"{self._control_plane()}/api/v1/baseline-probe",
            {
                "namespace": self.namespace,
                "baseline_reference_at": reference_at.isoformat(),
                "collector_started_at": self._collector_started_at.isoformat(),
            },
            {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {},
        )
        check_baseline(document)

    def start_timeline(self, scenario: ProductScenario) -> None:
        return None

    def set_traffic(self, enabled: bool) -> None:
        # Off before any on is already true: no traffic Pod exists on a fresh cluster.
        if enabled:
            self._exec(
                "kubectl", "--context", self._context, "apply", "-f", "-",
                stdin=yaml.safe_dump_all(traffic_manifest(self.traffic_duration)),
            )  # fmt: skip
            self._kubectl(
                "-n", TRAFFIC_NAMESPACE, "wait", "--for=condition=Ready",
                f"pod/{TRAFFIC_POD}", "--timeout=120s",
            )  # fmt: skip
            self._traffic = True
        elif self._traffic:
            self._kubectl(
                "-n", TRAFFIC_NAMESPACE, "delete", "pod", TRAFFIC_POD,
                "--ignore-not-found", "--wait=true", "--timeout=60s",
            )  # fmt: skip
            self._traffic = False

    def control(self) -> LiveClusterControl:
        return self.control_port

    def evidence(self) -> LiveEvidenceReader:
        return self.evidence_port

    def at_t0(self, t0: datetime) -> None:
        self._t0 = t0

    def _control_plane(self) -> str:
        url = self.control_plane_url or (
            f"http://127.0.0.1:{self.control_plane_port}" if self.control_plane_port else None
        )
        if url is None:
            raise RuntimeError("no control-plane URL for the revision schedule")
        return url

    def _reads(self) -> HttpControlPlaneReads:
        return HttpControlPlaneReads(self._control_plane(), self.http)

    def await_r1(self, scenario: ProductScenario) -> None:
        if self._t0 is None:
            raise RuntimeError("T0 was never set")
        url = self.prometheus_url or (
            f"http://127.0.0.1:{self.prometheus_port}" if self.prometheus_port else None
        )
        if url is None:
            raise RuntimeError("no Prometheus URL for R1 provenance")
        self._r1 = revisions.discover_r1(
            self._reads(),
            PrometheusActivations(url, self.http),
            self.control_port,
            t0=self._t0,
            config=self.schedule,
        )

    def run_r_early(self, scenario: ProductScenario) -> None:
        if self._r1 is None:
            raise RuntimeError("R1 was never discovered")
        headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}
        self._r_early = revisions.run_r_early(
            self._r1,
            self._reads(),
            HttpManualDiagnosis(self._control_plane(), headers, self.http),
            self.control_port,
            config=self.schedule,
        )

    def await_r2(self, scenario: ProductScenario) -> None:
        if self._r1 is None or self._r_early is None:
            raise RuntimeError("R2 awaited before R1 and R_early")
        self._r2 = revisions.await_r2(
            self._r1, self._reads(), self.control_port, config=self.schedule
        )

    def write_artifact(self, scenario: ProductScenario, result: RunResult) -> None:
        if self._r1 is None or self._r_early is None or self._r2 is None or self._run is None:
            raise RuntimeError("the artifact needs R1, R_early and R2 of this run")
        commit, protocol = artifact.provenance(self.git or artifact.LocalGit(self.root))
        summaries = sorted(
            self._reads().summaries(self._r1.incident_id),
            key=lambda item: int(item["revision_number"]),
        )
        accepted = [self._r1.diagnosis_id, self._r_early.diagnosis_id, self._r2]
        if [int(item["diagnosis_id"]) for item in summaries] != accepted:
            raise artifact.ArtifactError(
                f"the incident's revisions are not the accepted {accepted}"
            )
        run_ids = [item.get("run_id") for item in summaries]
        if not all(isinstance(item, str) and item for item in run_ids):
            raise artifact.ArtifactError("a revision has no run id for its provider tape")
        reads = self.artifact_reads or LiveArtifactReader(self.evidence_port._session_factory)
        document = artifact.build_artifact(
            scenario=scenario,
            commit=commit,
            protocol_commit=protocol,
            incident_id=self._r1.incident_id,
            onset=self._r1.onset,
            activations=self._r1.activations,
            timeline=result.timeline,
            summaries=summaries,
            requirements=reads.requirements(accepted),
            provider_tape=reads.provider_tape([str(item) for item in run_ids]),
        )
        bench_root = self.bench_root or self.root / ".local" / "product-bench"
        self.artifact_path = artifact.write_artifact(bench_root, self._run, document)

    def cluster_down(self) -> None:
        if self.before_teardown is not None:
            try:
                self.before_teardown(self)
            except Exception:  # evidence capture never blocks teardown
                logger.warning("before_teardown failed", exc_info=True)
        for process in self._forwards:
            process.terminate()
        self._forwards.clear()
        self._exec("kind", "delete", "cluster", "--name", self.cluster)


__all__ = [
    "CLUSTER",
    "PRODUCT_CONTROL_PLANE_ENV",
    "HttpControlPlaneReads",
    "HttpManualDiagnosis",
    "LiveArtifactReader",
    "LiveBackend",
    "LiveClusterControl",
    "LiveEvidenceReader",
    "PrometheusActivations",
    "StageNotImplemented",
    "baseline_faults",
    "product_control_plane_manifest",
    "product_environment",
]
