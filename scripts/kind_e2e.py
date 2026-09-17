#!/usr/bin/env python3
"""Run the canonical bad-rollout lifecycle against a real kind cluster.

The scenario deliberately uses the repository's manifests, control-plane
webhook, watcher, diagnosis API, and rollback commands. It is a release gate,
not a second RCA implementation.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = "agentic-sre"
CONTROL_PORT = 18000
ORDER_PORT = 18080
CONTROL_URL = f"http://127.0.0.1:{CONTROL_PORT}"
NAMESPACE = "sre-demo"


def command(args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one visible release-gate command."""
    print("$", " ".join(args), flush=True)
    return subprocess.run(args, cwd=ROOT, text=True, check=check, capture_output=True)


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return command(["kubectl", *args], check=check)


def wait_for(predicate: Any, *, timeout: float, description: str) -> Any:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except Exception as error:  # transient port-forward/API/readiness state
            last_error = str(error)
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for {description}; last error: {last_error}")


def http_json_at(base_url: str, method: str, path: str) -> Any:
    request = Request(f"{base_url}{path}", method=method)
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def http_json(method: str, path: str) -> Any:
    return http_json_at(CONTROL_URL, method, path)


def start_port_forward(service: str, port: int) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            "kubectl",
            "port-forward",
            "-n",
            NAMESPACE,
            f"svc/{service}",
            f"{port}:8000",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return process


def database_rows(sql: str) -> list[str]:
    result = kubectl(
        "exec",
        "deployment/postgres",
        "-n",
        NAMESPACE,
        "--",
        "psql",
        "-U",
        "postgres",
        "-d",
        "agentic_sre",
        "-At",
        "-c",
        sql,
    )
    return [line for line in result.stdout.splitlines() if line]


def payment_deployment_versions() -> list[dict[str, Any]]:
    rows = database_rows(
        "select body::text from object_versions "
        "where object_key = 'sre-demo/Deployment/payment-service' "
        "order by version_id"
    )
    return [json.loads(row) for row in rows]


def env_value(body: dict[str, Any], name: str) -> str | None:
    containers = body.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    if not isinstance(containers, list) or not containers:
        return None
    env = containers[0].get("env", [])
    if not isinstance(env, list):
        return None
    for item in env:
        if isinstance(item, dict) and item.get("name") == name:
            value = item.get("value")
            return str(value) if value is not None else None
    return None


def print_diagnostics() -> None:
    print("--- kind diagnostics ---")
    for args in (
        ("get", "pods", "-A", "-o", "wide"),
        ("get", "events", "-A", "--sort-by=.lastTimestamp"),
        ("get", "deployments", "-A"),
        ("logs", "deployment/control-plane", "-n", NAMESPACE, "--tail=120"),
    ):
        result = kubectl(*args, check=False)
        print(result.stdout or result.stderr)


def main() -> None:
    processes: list[subprocess.Popen[str]] = []
    try:
        # The target cluster is disposable and named explicitly, so reruns start
        # from an empty journal rather than inheriting a previous incident.
        command(["kind", "delete", "cluster", "--name", CLUSTER], check=False)
        command(["make", "cluster-up"])
        command(["make", "deploy"])

        control_forward = start_port_forward("control-plane", CONTROL_PORT)
        processes.append(control_forward)
        wait_for(lambda: http_json("GET", "/ready"), timeout=60, description="control-plane ready")

        baseline = http_json("POST", "/api/v1/cluster/snapshot")
        print("baseline snapshot:", baseline)
        baseline_versions = payment_deployment_versions()
        if not baseline_versions:
            raise AssertionError("baseline payment Deployment was not journaled")

        order_forward = start_port_forward("order-service", ORDER_PORT)
        processes.append(order_forward)
        wait_for(
            lambda: (
                http_json_at(f"http://127.0.0.1:{ORDER_PORT}", "GET", "/health")
                if order_forward.poll() is None
                else None
            ),
            timeout=30,
            description="order-service port-forward",
        )
        load = subprocess.Popen(
            [
                ".venv/bin/python",
                "-m",
                "workload.load_generator",
                "--base-url",
                f"http://127.0.0.1:{ORDER_PORT}",
                "--rate",
                "10",
                "--duration",
                "90",
                "--seed",
                "42",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        processes.append(load)

        command(
            [
                "kubectl",
                "set",
                "env",
                "deployment/payment-service",
                "-n",
                NAMESPACE,
                "FAULT_PAYMENT_DELAY_MS=2500",
            ]
        )
        command(
            [
                "kubectl",
                "rollout",
                "status",
                "deployment/payment-service",
                "-n",
                NAMESPACE,
                "--timeout=120s",
            ]
        )
        http_json("POST", "/api/v1/cluster/snapshot")

        def incident() -> dict[str, Any] | None:
            items = http_json("GET", "/api/v1/incidents")
            return items[0] if items else None

        current_incident = wait_for(
            incident,
            timeout=150,
            description="Prometheus -> Alertmanager -> control-plane incident",
        )
        incident_id = str(current_incident["incident_id"])
        diagnosis = http_json("POST", f"/api/v1/incidents/{incident_id}/diagnosis")
        expected = {"kind": "Deployment", "name": "payment-service", "namespace": NAMESPACE}
        if diagnosis.get("root_cause") != expected:
            raise AssertionError(f"expected {expected}, got {diagnosis.get('root_cause')}")
        if diagnosis.get("confidence") != "VERIFIED":
            raise AssertionError(f"expected VERIFIED, got {diagnosis.get('confidence')}")
        if not diagnosis.get("evidence"):
            raise AssertionError("diagnosis has no persisted evidence")
        if "causal_path" not in diagnosis:
            raise AssertionError("diagnosis omitted its structured causal path field")
        print("diagnosis:", json.dumps(diagnosis, sort_keys=True))

        event_count = int(database_rows("select count(*) from event_versions")[0])
        if event_count <= 0:
            raise AssertionError("no Kubernetes Event was persisted")
        print("persisted Kubernetes Event versions:", event_count)

        command(["kubectl", "rollout", "undo", "deployment/payment-service", "-n", NAMESPACE])
        command(
            [
                "kubectl",
                "rollout",
                "status",
                "deployment/payment-service",
                "-n",
                NAMESPACE,
                "--timeout=120s",
            ]
        )
        http_json("POST", "/api/v1/cluster/snapshot")
        versions = payment_deployment_versions()
        values = [env_value(body, "FAULT_PAYMENT_DELAY_MS") for body in versions]
        if len(values) < 3 or values[0] != values[-1] or "2500" not in values:
            raise AssertionError(f"expected A -> B -> A journal values, got {values}")
        print("payment rollout journal values:", values)

        def resolved() -> dict[str, Any] | None:
            current = http_json("GET", f"/api/v1/incidents/{incident_id}")
            return current if current.get("status") in {"RESOLVED", "CLOSED"} else None

        wait_for(resolved, timeout=150, description="Alertmanager resolved delivery")
        replay = http_json("POST", f"/api/v1/incidents/{incident_id}/diagnosis")
        if replay.get("root_cause") != expected or replay.get("confidence") != "VERIFIED":
            raise AssertionError("resolved replay changed the expected deterministic diagnosis")
        print("resolved replay: stable VERIFIED payment Deployment")
    except Exception:
        print_diagnostics()
        raise
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        command(["kind", "delete", "cluster", "--name", CLUSTER], check=False)


if __name__ == "__main__":
    main()
