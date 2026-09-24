"""Exercise bounded M18A telemetry readers against an existing Kind stack.

The script emits only bounded OTLP fixture telemetry for the Loki and Tempo
normalizer proofs. Its application request is a read-only GET of a fixed,
nonexistent order ID so Prometheus has a stable traffic signal without writing
application data. It does not create or mutate Kubernetes objects.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

from packages.rca.engine import build_case
from packages.rca.investigation.normalizers import normalize_observation
from packages.rca.investigation.prometheus import PrometheusConfig, PrometheusMetricsReader
from packages.rca.investigation.tempo import TempoConfig, TempoTraceReader
from packages.rca.investigation.tools import default_tools
from packages.rca.live import KubernetesClusterReader, LiveSource, LokiLogReader
from packages.rca.model import (
    Alert,
    AuthorizedQuery,
    EntityRef,
    GapDimension,
    GapResolvability,
    InformationGap,
    InvestigationQuery,
)

PORT_FORWARDS = (
    ("observability", "svc/prometheus", 19090, 9090),
    ("observability", "svc/loki", 13100, 3100),
    ("observability", "svc/tempo", 13200, 3200),
    ("observability", "svc/otel-collector", 14317, 4317),
    ("sre-demo", "svc/order-service", 18080, 8000),
)
FIXTURE_ORDER_ID = "00000000-0000-4000-8000-000000000000"
_PORT_FORWARD_PROCESSES: list[subprocess.Popen[bytes]] = []


def _start_port_forwards() -> dict[str, int]:
    local_ports: dict[str, int] = {}
    for namespace, target, local_port, remote_port in PORT_FORWARDS:
        process = subprocess.Popen(
            [
                "kubectl",
                "port-forward",
                "-n",
                namespace,
                target,
                f"{local_port}:{remote_port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _PORT_FORWARD_PROCESSES.append(process)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"kubectl port-forward failed for {namespace}/{target}")
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError(f"kubectl port-forward timed out for {namespace}/{target}")
        local_ports[target.rsplit("/", maxsplit=1)[-1]] = local_port
    return local_ports


def _stop_port_forwards() -> None:
    for process in reversed(_PORT_FORWARD_PROCESSES):
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    _PORT_FORWARD_PROCESSES.clear()


def _gap(capability: str, dimension: GapDimension, target: EntityRef) -> InformationGap:
    return InformationGap(
        gap_id=f"m18a-live-{capability}",
        dimension=dimension,
        missing_fact=f"bounded {capability} observation for the M18A live fixture",
        entity_scope=(target,),
        authorized_queries=(AuthorizedQuery(capability=capability, target=target),),
        candidate_tools=(capability,),
        resolvability=GapResolvability.RESOLVABLE,
    )


def _finding_summary(normalized: Any) -> list[dict[str, Any]]:
    return [
        {
            "kind": finding.kind.value,
            "entity": finding.entity.canonical,
            "evidence_ids": list(finding.evidence_ids),
            "runtime_provenance": {
                key: value for key, value in finding.details.items() if key.startswith("runtime_")
            },
        }
        for finding in normalized.findings
    ]


def _observation_summary(observation: Any, normalized: Any) -> dict[str, Any]:
    context = normalized.observation.runtime
    if context is None:
        raise RuntimeError(f"{observation.capability} did not produce runtime context")
    query = context.query
    if query.limit > 32 or query.effective_end - query.effective_start > timedelta(hours=1):
        raise RuntimeError(f"{observation.capability} exceeded the M18A query bound")
    return {
        "observation_id": observation.observation_id,
        "pillar": context.pillar.value,
        "capability": context.capability,
        "target": query.target.canonical,
        "state": context.state.value,
        "query": {
            "descriptor_id": query.descriptor_id,
            "template_id": query.template_id,
            "requested_start": query.requested_start.isoformat(),
            "requested_end": query.requested_end.isoformat(),
            "effective_start": query.effective_start.isoformat(),
            "effective_end": query.effective_end.isoformat(),
            "result_limit": query.limit,
        },
        "source_observation_ids": list(context.source_observation_ids),
        "findings": _finding_summary(normalized),
    }


def _request_missing_order(order_port: int) -> int:
    request = Request(f"http://127.0.0.1:{order_port}/orders/{FIXTURE_ORDER_ID}", method="GET")
    try:
        with urlopen(request, timeout=5) as response:
            return int(response.status)
    except HTTPError as error:
        return error.code


def _live_source(
    *,
    observed_at: datetime,
    onset: datetime,
    prom_port: int,
    loki_port: int,
    tempo_port: int,
    objects: list[dict[str, Any]],
    events: list[dict[str, Any]],
    alert_service: str,
    scoped_objects: list[dict[str, Any]] | None = None,
) -> LiveSource:
    return LiveSource(
        incident="m18a-live-validation",
        alert_items=[
            Alert(
                name="M18A-live-validation",
                service=alert_service,
                namespace="sre-demo",
                starts_at=onset,
            )
        ],
        journal=(),
        current_objects=objects if scoped_objects is None else scoped_objects,
        event_bodies=events,
        observed_at=observed_at,
        prometheus_reader=PrometheusMetricsReader(
            PrometheusConfig(f"http://127.0.0.1:{prom_port}")
        ),
        loki_reader=LokiLogReader(f"http://127.0.0.1:{loki_port}"),
        tempo_reader=TempoTraceReader(TempoConfig(f"http://127.0.0.1:{tempo_port}")),
    )


def _execute(
    source: LiveSource,
    *,
    capability: str,
    dimension: GapDimension,
    target: EntityRef,
    start: datetime,
    end: datetime,
    limit: int = 32,
) -> tuple[Any, Any]:
    gap = _gap(capability, dimension, target)
    case = build_case(source)
    tool = cast(Any, default_tools(source.investigation_backend())[capability])
    observation = tool.execute_query(
        case,
        gap,
        target,
        InvestigationQuery(start=start, end=end, limit=limit),
    )
    return observation, normalize_observation(observation, case=case, gap=gap)


def _emit_loki_fixture(collector_port: int) -> None:
    resource = Resource.create(
        {
            "service.name": "payment-service",
            "service.version": "m18a-live-fixture",
            "k8s.namespace.name": "sre-demo",
            "k8s.deployment.name": "payment-service",
        }
    )
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(
        SimpleLogRecordProcessor(
            OTLPLogExporter(endpoint=f"127.0.0.1:{collector_port}", insecure=True)
        )
    )
    logger = logging.getLogger(f"m18a-live-{uuid4()}")
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    handler = LoggingHandler(level=logging.ERROR, logger_provider=provider)
    logger.addHandler(handler)
    try:
        logger.error("connection refused redis")
        provider.force_flush(timeout_millis=10_000)
    finally:
        logger.removeHandler(handler)
        provider.shutdown()


def _emit_tempo_fixture(collector_port: int) -> tuple[str, datetime]:
    """Send one linked client/server span pair; no Kubernetes/app request is made."""
    now = datetime.now(UTC)
    client_provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "order-service",
                "service.version": "m18a-live-fixture",
                "k8s.namespace.name": "sre-demo",
                "k8s.deployment.name": "order-service",
            }
        )
    )
    client_provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"127.0.0.1:{collector_port}", insecure=True))
    )
    client = client_provider.get_tracer("m18a-live-validation").start_span(
        "GET /payments", kind=SpanKind.CLIENT
    )
    client_context = client.get_span_context()
    client.set_attribute("http.request.method", "GET")
    client.set_attribute("server.address", "payment-service")
    client.set_attribute("http.response.status_code", 503)
    client_context = client.get_span_context()

    server_provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "payment-service",
                "service.version": "m18a-live-fixture",
                "k8s.namespace.name": "sre-demo",
                "k8s.deployment.name": "payment-service",
            }
        )
    )
    server_provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"127.0.0.1:{collector_port}", insecure=True))
    )
    try:
        parent = trace.NonRecordingSpan(client_context)
        server = server_provider.get_tracer("m18a-live-validation").start_span(
            "GET /payments",
            context=trace.set_span_in_context(parent),
            kind=SpanKind.SERVER,
        )
        server.set_attribute("http.request.method", "GET")
        server.set_attribute("http.response.status_code", 503)
        server.set_status(Status(StatusCode.ERROR, "bounded M18A fixture error"))
        server.end()
    finally:
        client.end()
        client_provider.force_flush(timeout_millis=10_000)
        server_provider.force_flush(timeout_millis=10_000)
        client_provider.shutdown()
        server_provider.shutdown()
    return f"{client_context.trace_id:032x}", now


def _run_live_validation() -> dict[str, Any]:
    print("M18A live validation: connecting to existing Kind services", file=sys.stderr, flush=True)
    ports = _start_port_forwards()
    try:
        cluster = KubernetesClusterReader(chaos_namespaces=())
        listing = cluster.list_objects(["sre-demo"])
        if listing.failed_scopes:
            raise RuntimeError(
                f"Kubernetes read-only inventory had failures: {listing.failed_scopes}"
            )
        events = cluster.list_events(["sre-demo"])
        objects = list(listing.objects)
        output: dict[str, Any] = {
            "contract_version": "m18a.v1",
            "environment": "existing Kind cluster; observability and sre-demo namespaces",
            "provider_calls": 0,
            "safety": {
                "kubernetes_writes": 0,
                "secret_access": 0,
                "out_of_policy_execution": 0,
                "autonomous_remediation": 0,
                "application_data_writes": 0,
                "fixture_telemetry_records_emitted": 3,
            },
            "fixtures": {
                "prometheus": "read-only GET to a fixed nonexistent order ID; no order is created",
                "loki": "one synthetic connection-refused OTLP log through the live collector",
                "tempo": "one synthetic linked CLIENT/SERVER OTLP span pair through the live collector; no application request",
            },
            "pillars": {},
        }

        # Warm the real scrape path, then measure a low-rate normal window.
        print("M18A live validation: Prometheus normal metric path", file=sys.stderr, flush=True)
        for _ in range(10):
            if _request_missing_order(ports["order-service"]) != 404:
                raise RuntimeError("read-only Prometheus fixture request did not return 404")
            time.sleep(2)
        normal_start = datetime.now(UTC)
        for _ in range(30):
            if _request_missing_order(ports["order-service"]) != 404:
                raise RuntimeError("read-only Prometheus fixture request did not return 404")
            time.sleep(2)
        normal_end = datetime.now(UTC)
        normal_onset = normal_start + timedelta(seconds=30)
        service = EntityRef(kind="Service", name="order-service", namespace="sre-demo")
        normal_source = _live_source(
            observed_at=normal_end,
            onset=normal_onset,
            prom_port=ports["prometheus"],
            loki_port=ports["loki"],
            tempo_port=ports["tempo"],
            objects=objects,
            events=events,
            alert_service="order-service",
        )
        normal_observation, normal_result = _execute(
            normal_source,
            capability="traffic",
            dimension=GapDimension.METRIC_BASELINE,
            target=service,
            start=normal_start - timedelta(seconds=20),
            end=normal_end,
        )
        if normal_observation.runtime is None or not normal_observation.payload.get("traffic"):
            raise RuntimeError("Prometheus normal traffic proof returned no typed samples")
        if normal_observation.runtime.state.value != "OBSERVED_NORMAL":
            raise RuntimeError(
                "Prometheus measured low-rate traffic was not classified OBSERVED_NORMAL: "
                f"{normal_observation.runtime.state.value}"
            )

        # Increase read-only request rate enough to cross the existing fixed 1.5x rule.
        print("M18A live validation: Prometheus abnormal metric path", file=sys.stderr, flush=True)
        baseline_start = datetime.now(UTC)
        for _ in range(10):
            if _request_missing_order(ports["order-service"]) != 404:
                raise RuntimeError("read-only Prometheus fixture request did not return 404")
            time.sleep(1)
        abnormal_onset = datetime.now(UTC)
        for _ in range(120):
            if _request_missing_order(ports["order-service"]) != 404:
                raise RuntimeError("read-only Prometheus fixture request did not return 404")
            time.sleep(0.25)
        time.sleep(12)
        abnormal_end = datetime.now(UTC)
        abnormal_source = _live_source(
            observed_at=abnormal_end,
            onset=abnormal_onset,
            prom_port=ports["prometheus"],
            loki_port=ports["loki"],
            tempo_port=ports["tempo"],
            objects=objects,
            events=events,
            alert_service="order-service",
        )
        abnormal_observation, abnormal_result = _execute(
            abnormal_source,
            capability="traffic",
            dimension=GapDimension.METRIC_CHANGE,
            target=service,
            start=baseline_start - timedelta(seconds=20),
            end=abnormal_end,
        )
        abnormal_findings = _finding_summary(abnormal_result)
        if (
            abnormal_observation.runtime is None
            or abnormal_observation.runtime.state.value != "OBSERVED_ABNORMAL"
            or not any(item["kind"] == "TRAFFIC_INCREASE" for item in abnormal_findings)
        ):
            raise RuntimeError(
                "Prometheus abnormal traffic did not produce an OBSERVED_ABNORMAL "
                "TRAFFIC_INCREASE Finding"
            )
        output["pillars"]["prometheus"] = {
            "normal": _observation_summary(normal_observation, normal_result),
            "abnormal": _observation_summary(abnormal_observation, abnormal_result),
            "actual_sample_count": len(abnormal_observation.payload.get("traffic", [])),
            "query_bounds_seconds": 3600,
        }
        order_pod_name = next(
            item["metadata"]["name"]
            for item in objects
            if item.get("kind") == "Pod"
            and item.get("metadata", {}).get("labels", {}).get("app") == "order-service"
        )
        order_pod = EntityRef(kind="Pod", name=order_pod_name, namespace="sre-demo")
        resource_observation, resource_result = _execute(
            abnormal_source,
            capability="resource_pressure",
            dimension=GapDimension.METRIC_BASELINE,
            target=order_pod,
            start=abnormal_end - timedelta(minutes=2),
            end=abnormal_end,
        )
        output["pillars"]["prometheus"]["resource_pressure_probe"] = _observation_summary(
            resource_observation, resource_result
        )
        output["pillars"]["prometheus"]["resource_pressure_probe"]["metric_records"] = len(
            resource_observation.payload.get("resource_pressure", [])
        )

        # Loki: send one explicit fixture record through OTLP Collector, then read
        # it through the bounded logs capability with a narrow alert scope.
        print("M18A live validation: Loki typed mechanism path", file=sys.stderr, flush=True)
        _emit_loki_fixture(ports["otel-collector"])
        time.sleep(3)
        loki_end = datetime.now(UTC)
        loki_start = loki_end - timedelta(seconds=30)
        loki_objects = [
            item
            for item in objects
            if (
                item.get("kind") == "Deployment"
                and item.get("metadata", {}).get("name") == "payment-service"
            )
            or item.get("kind") == "Service"
            or (
                item.get("kind") == "ConfigMap"
                and item.get("metadata", {}).get("name") == "workload-config"
            )
            or (
                item.get("kind") == "Pod"
                and item.get("metadata", {}).get("labels", {}).get("app") == "payment-service"
            )
        ]
        payment = EntityRef(kind="Deployment", name="payment-service", namespace="sre-demo")
        loki_source = _live_source(
            observed_at=loki_end,
            onset=loki_start,
            prom_port=ports["prometheus"],
            loki_port=ports["loki"],
            tempo_port=ports["tempo"],
            objects=objects,
            events=events,
            alert_service="payment-service",
            scoped_objects=loki_objects,
        )
        loki_observation, loki_result = _execute(
            loki_source,
            capability="logs",
            dimension=GapDimension.LOG_ERROR_PATTERN,
            target=payment,
            start=loki_start,
            end=loki_end,
        )
        loki_findings = _finding_summary(loki_result)
        if (
            loki_observation.runtime is None
            or not loki_observation.payload.get("logs")
            or not any(item["kind"] == "DEPENDENCY_ERRORS" for item in loki_findings)
        ):
            raise RuntimeError(
                "Loki fixture did not produce a normalized DEPENDENCY_ERRORS Finding"
            )
        output["pillars"]["loki"] = _observation_summary(loki_observation, loki_result)

        # Tempo: export a linked failure pair to the live OTLP collector, then
        # allow the real Tempo search index to catch up before a narrow read.
        print("M18A live validation: Tempo direction and failure path", file=sys.stderr, flush=True)
        trace_id, tempo_started = _emit_tempo_fixture(ports["otel-collector"])
        time.sleep(12)
        tempo_end = datetime.now(UTC)
        tempo_source = _live_source(
            observed_at=tempo_end,
            onset=tempo_started - timedelta(seconds=2),
            prom_port=ports["prometheus"],
            loki_port=ports["loki"],
            tempo_port=ports["tempo"],
            objects=objects,
            events=events,
            alert_service="order-service",
        )
        tempo_observation, tempo_result = _execute(
            tempo_source,
            capability="runtime_traces",
            dimension=GapDimension.DEPENDENCY_HEALTH,
            target=payment,
            start=tempo_started - timedelta(seconds=1),
            end=tempo_started + timedelta(seconds=3),
        )
        tempo_findings = _finding_summary(tempo_result)
        call_facts = tempo_observation.payload.get("trace_call_facts", [])
        if (
            tempo_observation.runtime is None
            or not tempo_observation.payload.get("traces")
            or not any(item["kind"] == "DEPENDENCY_ERRORS" for item in tempo_findings)
            or not any(
                item.get("caller_service") == "order-service"
                and item.get("callee_service") == "payment-service"
                for item in call_facts
            )
        ):
            raise RuntimeError(
                "Tempo fixture did not produce a directional caller/callee "
                "DEPENDENCY_ERRORS Finding"
            )
        output["pillars"]["tempo"] = {
            **_observation_summary(tempo_observation, tempo_result),
            "fixture_trace_id": trace_id,
            "call_facts": [
                {
                    "caller": fact.get("caller_service"),
                    "callee": fact.get("callee_service"),
                    "direction_basis": fact.get("direction_basis"),
                    "caller_outcome": fact.get("caller_outcome"),
                    "callee_outcome": fact.get("callee_outcome"),
                    "caller_duration_seconds": fact.get("caller_duration_seconds"),
                    "callee_duration_seconds": fact.get("callee_duration_seconds"),
                }
                for fact in call_facts
            ],
        }
        return output
    finally:
        _stop_port_forwards()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        help="optional local JSON output path; defaults to stdout",
    )
    args = parser.parse_args()
    report = _run_live_validation()
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out is None:
        sys.stdout.write(rendered)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
        print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
