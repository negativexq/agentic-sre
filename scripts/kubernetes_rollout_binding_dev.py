"""Isolated Kubernetes DEV capture using real workload code and a driver-defined symptom.

SQLite and an in-memory event publisher replace non-HTTP dependencies. Captured
Kubernetes/OTLP evidence is evaluated offline; this is not a production CP run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from packages.evals.frozen_request_binding import FrozenLog, FrozenRequestEvidence, FrozenSpan
from packages.evals.kubernetes_dev_watch import DevelopmentObjectWatch, apply_watch_lifecycle
from packages.rca.investigation.tempo import parse_tempo_trace_json
from packages.rca.model import EntityRef, Lifecycle, ObjectVersion, TraceSpanObservation
from packages.rca.provider_adapter import DevelopmentTransportProbe

BOOTSTRAP = """import os
from packages.storage import create_database_engine
from workload.common.models import WorkloadBase
WorkloadBase.metadata.create_all(create_database_engine(os.environ['DATABASE_URL']))
if os.environ['DEV_ROLE'] == 'order':
    import workload.order_service.app as module
    from packages.telemetry.runtime import create_runtime
    from workload.common.adapters import InMemoryEventPublisher
    module.create_runtime = lambda service_name, **kwargs: create_runtime(os.environ['DEV_CALLER_NAME'], **kwargs)
    app = module.create_app(event_publisher=InMemoryEventPublisher())
else:
    from workload.payment_service.app import app
import uvicorn
uvicorn.run(app, host='0.0.0.0', port=8000)
"""


def now() -> datetime:
    return datetime.now(UTC)


def kubectl(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["kubectl", *args], input=input_text, text=True, capture_output=True, check=True, timeout=30
    ).stdout


def manifests(namespace: str, caller: str, image: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace, "labels": {"agentic-sre-dev-owned": "rollout-binding"}},
        },
    ]
    for name, role in ((caller, "order"), ("payment-service", "payment")):
        env: list[dict[str, Any]] = [
            {"name": k, "value": v}
            for k, v in {
                "DATABASE_URL": "sqlite:////tmp/workload.db",
                "DEV_ROLE": role,
                "DEV_CALLER_NAME": caller,
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector.observability.svc.cluster.local:4317",
                "OTEL_METRIC_EXPORT_INTERVAL": "5000",
                "PYTHONPATH": "/app",
            }.items()
        ]
        if role == "order":
            env.append({"name": "PAYMENT_SERVICE_URL", "value": "http://payment-service:8000"})
        env += [
            {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
            {
                "name": "POD_NAMESPACE",
                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
            },
            {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
            {
                "name": "OTEL_RESOURCE_ATTRIBUTES",
                "value": f"k8s.namespace.name=$(POD_NAMESPACE),k8s.pod.name=$(POD_NAME),k8s.deployment.name={name}",
            },
        ]
        items += [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": name}},
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
                    },
                    "template": {
                        "metadata": {"labels": {"app": name}},
                        "spec": {
                            "terminationGracePeriodSeconds": 2,
                            "containers": [
                                {
                                    "name": name,
                                    "image": image,
                                    "imagePullPolicy": "Never",
                                    "command": ["python", "/dev-bootstrap/bootstrap.py"],
                                    "env": env,
                                    "ports": [{"containerPort": 8000}],
                                    "readinessProbe": {
                                        "httpGet": {"path": "/ready", "port": 8000},
                                        "initialDelaySeconds": 15,
                                        "periodSeconds": 2,
                                    },
                                    "resources": {
                                        "requests": {"cpu": "50m", "memory": "128Mi"},
                                        "limits": {"cpu": "500m", "memory": "512Mi"},
                                    },
                                }
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {"selector": {"app": name}, "ports": [{"port": 8000, "targetPort": 8000}]},
            },
        ]
    return {"apiVersion": "v1", "kind": "List", "items": items}


def capture(output: Path, image: str) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Use a fresh output directory")
    token = uuid4().hex[:8]
    namespace = f"binding-dev-{token}"
    caller = f"binding-dev-order-{token}"
    output.mkdir(parents=True)
    protocol = {
        "namespace": namespace,
        "caller": caller,
        "image": image,
        "profile": "REAL_K8S_DEV_OFFLINE_RCA_DRIVER_SYMPTOM",
        "harness_revision": "BOOTSTRAP_IN_IMAGE_READINESS_INITIAL_DELAY_15S",
        "deletion_evidence": "KUBERNETES_WATCH_DELETE",
        "driver_alert": "First POST /orders response >=500 after 10 successful baseline requests; remains latched until capture ends",
        "baseline_requests": 10,
        "fault_requests": 30,
        "alert_channel": "INDEPENDENT_DRIVER_NOT_PROMETHEUS",
        "non_http_dependencies": "POD_LOCAL_SQLITE_AND_IN_MEMORY_EVENT_PUBLISHER",
        "production_cp_used": False,
        "source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    spec = manifests(namespace, caller, image)
    (output / "manifests.json").write_text(json.dumps(spec, indent=2) + "\n")
    versions: list[ObjectVersion] = []
    ledger = []
    failures = []
    stop = threading.Event()
    previous: dict[str, dict[str, Any]] = {}
    initialized = False
    snapshots = []

    def snapshot() -> None:
        nonlocal previous, initialized
        body = json.loads(
            kubectl(
                "-n", namespace, "get", "deployment,replicaset,pod,service,configmap", "-o", "json"
            )
        )
        at = now()
        snapshots.append({"observed_at": at.isoformat(), "listing": body})
        current = {f"{r['kind']}/{r['metadata']['name']}": r for r in body["items"]}
        for key, item in current.items():
            if previous.get(key) != item:
                kind = (
                    Lifecycle.UPDATED
                    if key in previous
                    else Lifecycle.CREATED
                    if initialized
                    else Lifecycle.OBSERVED
                )
                versions.append(
                    ObjectVersion(
                        entity=EntityRef(
                            namespace=namespace, kind=item["kind"], name=item["metadata"]["name"]
                        ),
                        uid=item["metadata"]["uid"],
                        observed_at=at,
                        body=item,
                        evidence_id=f"dev-object:{len(versions)}",
                        lifecycle=kind,
                    )
                )
        previous = current
        initialized = True

    def watch() -> None:
        while not stop.wait(0.8):
            try:
                snapshot()
            except Exception as exc:
                failures.append(type(exc).__name__)

    forward = None
    thread = None
    watches: list[DevelopmentObjectWatch] = []
    owned = json.loads(
        kubectl("create", "-f", "-", "-o", "json", input_text=json.dumps(spec["items"][0]))
    )
    owned_uid = owned["metadata"]["uid"]
    (output / "runner.py").write_bytes(Path(__file__).read_bytes())
    try:
        kubectl("apply", "-f", "-", input_text=json.dumps({**spec, "items": spec["items"][1:]}))
        for dep in (caller, "payment-service"):
            subprocess.run(
                [
                    "kubectl",
                    "-n",
                    namespace,
                    "rollout",
                    "status",
                    f"deployment/{dep}",
                    "--timeout=120s",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=130,
            )
        snapshot()
        for resource in ("pods", "replicasets"):
            observer = DevelopmentObjectWatch(namespace, resource, output)
            watches.append(observer)
            observer.start()
        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        log = (output / "port-forward.log").open("w")
        forward = subprocess.Popen(
            ["kubectl", "-n", namespace, "port-forward", f"service/{caller}", "18089:8000"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        for _ in range(30):
            if "Forwarding from" in (output / "port-forward.log").read_text():
                break
            time.sleep(0.2)

        def request(phase: str) -> int:
            rid = uuid4().hex
            started = now()
            req = Request(
                "http://127.0.0.1:18089/orders",
                data=json.dumps(
                    {"customer_id": f"dev-{rid}", "amount_cents": 100, "currency": "TRY"}
                ).encode(),
                headers={"Content-Type": "application/json", "x-request-id": rid},
                method="POST",
            )
            try:
                with urlopen(req, timeout=10) as response:
                    status = response.status
                    response.read()
            except HTTPError as exc:
                status = exc.code
                exc.read()
            ledger.append(
                {
                    "request_id": rid,
                    "phase": phase,
                    "start": started.isoformat(),
                    "end": now().isoformat(),
                    "status": status,
                }
            )
            return int(status)

        if any(request("baseline") != 201 for _ in range(10)):
            raise ValueError("Baseline validation failed")
        baseline_end = now()
        old_pods = {
            r["metadata"]["name"]
            for r in previous.values()
            if r["kind"] == "Pod" and r["metadata"]["labels"].get("app") == "payment-service"
        }
        injection = now()
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "payment-service",
                                "env": [
                                    {"name": "FAULT_PAYMENT_DELAY_MS", "value": "dev-invalid-delay"}
                                ],
                            }
                        ]
                    }
                }
            }
        }
        kubectl(
            "-n",
            namespace,
            "patch",
            "deployment",
            "payment-service",
            "--type=strategic",
            "-p",
            json.dumps(patch),
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            pods = json.loads(
                kubectl("-n", namespace, "get", "pods", "-l", "app=payment-service", "-o", "json")
            )["items"]
            if (
                pods
                and not old_pods & {r["metadata"]["name"] for r in pods}
                and any(
                    c.get("state", {}).get("waiting") or c.get("restartCount", 0)
                    for r in pods
                    for c in r.get("status", {}).get("containerStatuses", [])
                )
            ):
                break
            time.sleep(1)
        else:
            raise TimeoutError("Failed rollout not observed")
        for _ in range(30):
            request("fault")
        events = []
        for _ in range(20):
            events = json.loads(kubectl("-n", namespace, "get", "events", "-o", "json"))["items"]
            if any(e.get("reason") == "BackOff" for e in events):
                break
            time.sleep(1)
        stop.set()
        thread.join(timeout=10)
        snapshot()
        for observer in watches:
            observer.stop()
        if any(observer.errors for observer in watches):
            raise ValueError("Lifecycle watch failed")
        versions = apply_watch_lifecycle(
            versions, [row for observer in watches for row in observer.records]
        )
        if not old_pods <= {v.entity.name for v in versions if v.lifecycle is Lifecycle.DELETED}:
            raise ValueError("Old revision removal lacks exact Kubernetes DELETE watch evidence")
        start = datetime.fromisoformat(ledger[0]["start"])
        cutoff = now()
        (output / "capture-journal.json").write_text(json.dumps(snapshots, indent=2) + "\n")
        (output / "object-versions.json").write_text(
            json.dumps([v.model_dump(mode="json") for v in versions], indent=2) + "\n"
        )
        (output / "events.json").write_text(json.dumps(events, indent=2) + "\n")
        (output / "driver-ledger.json").write_text(json.dumps(ledger, indent=2) + "\n")
        (output / "timeline.json").write_text(
            json.dumps(
                {
                    "start": start.isoformat(),
                    "baseline_end": baseline_end.isoformat(),
                    "injection": injection.isoformat(),
                    "cutoff": cutoff.isoformat(),
                    "poll_failures": failures,
                },
                indent=2,
            )
            + "\n"
        )
        probe = DevelopmentTransportProbe("http://127.0.0.1:13100", "http://127.0.0.1:13200")
        rawdir = output / "provider-reads"
        rawdir.mkdir()
        spans: list[TraceSpanObservation] = []
        logs = []
        for attempt in range(18):
            captured = now()
            logs = probe.error_logs(caller, start, captured)
            search = probe.search(caller, start, captured)
            (rawdir / f"search-{attempt}.json").write_text(json.dumps(search) + "\n")
            rawtraces = search.get("traces", [])
            if not isinstance(rawtraces, list):
                raise ValueError("Invalid DEV search result")
            spans = []
            for row in rawtraces:
                tid = str(row["traceID"])
                payload = probe.trace(tid)
                (rawdir / f"trace-{tid}.json").write_text(json.dumps(payload) + "\n")
                spans.extend(parse_tempo_trace_json(payload))
            ids = {
                s.semantic_attributes.get("http.request.header.x_request_id")
                for s in spans
                if s.span_kind == "SERVER" and s.service == caller
            }
            seen = {r.attributes.get("request_id") for r in logs if r.attributes.get("error.type")}
            if {r["request_id"] for r in ledger} <= ids and {
                r["request_id"] for r in ledger if r["phase"] == "fault"
            } <= seen:
                break
            time.sleep(3)
        else:
            raise TimeoutError("Remote reference capture incomplete")
        captured = now()
        evidence = FrozenRequestEvidence(
            incident_id=namespace,
            run_id=namespace,
            episode_start=start,
            episode_end=cutoff,
            frozen_cutoff=captured,
            caller_service=caller,
            configured_peer="payment-service",
            logs=tuple(
                FrozenLog(
                    incident_id=namespace,
                    run_id=namespace,
                    source_reference=f"dev-loki:{r.evidence_id}",
                    captured_at=captured,
                    record=r,
                )
                for r in logs
            ),
            spans=tuple(
                FrozenSpan(
                    incident_id=namespace,
                    run_id=namespace,
                    source_reference=f"dev-tempo:{s.evidence_id}",
                    captured_at=captured,
                    record=s,
                )
                for s in spans
            ),
        )
        (output / "remote-evidence.json").write_text(evidence.model_dump_json(indent=2) + "\n")
        result = {
            "namespace": namespace,
            "caller": caller,
            "baseline_statuses": dict(
                Counter(r["status"] for r in ledger if r["phase"] == "baseline")
            ),
            "fault_statuses": dict(Counter(r["status"] for r in ledger if r["phase"] == "fault")),
            "logs": len(logs),
            "spans": len(spans),
            "object_versions": len(versions),
            "warning_events": [r.get("reason") for r in events if r.get("type") == "Warning"],
            "poll_failures": failures,
            "production_cp_used": False,
        }
        (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        stop.set()
        if thread:
            thread.join(timeout=10)
        for observer in watches:
            observer.stop()
        if forward:
            forward.terminate()
            forward.wait(timeout=10)
        # Delete only this freshly created, owned namespace; retained files are immutable inputs.
        for name, value in (
            ("driver-ledger.json", ledger),
            ("capture-journal.json", snapshots),
            ("object-versions.json", [v.model_dump(mode="json") for v in versions]),
        ):
            path = output / name
            if not path.exists():
                path.write_text(json.dumps(value, indent=2) + "\n")
        current = json.loads(kubectl("get", "namespace", namespace, "-o", "json"))
        if current["metadata"]["uid"] != owned_uid:
            raise RuntimeError("Owned DEV namespace identity changed; cleanup refused")
        kubectl("delete", "namespace", namespace, "--wait=false")
        (output / "cleanup.json").write_text(
            json.dumps({"namespace_delete_requested": namespace, "at": now().isoformat()}) + "\n"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--image", default="agentic-sre/binding-dev:85fb474-bootstrap-v1")
    args = p.parse_args()
    print(json.dumps(capture(args.output, args.image), indent=2))


if __name__ == "__main__":
    main()
