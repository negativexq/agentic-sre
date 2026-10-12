"""Bounded payment telemetry A/B: real HTTP app, PostgreSQL and Tempo.

Uses an experiment-owned database and unique trace service names. Requires the
existing lab PostgreSQL and Tempo forwards at localhost:13200 and :14317.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient
from workload.common.models import WorkloadBase
from workload.payment_service import app as payment

from packages.evals.service_effect_shadow import PG
from packages.telemetry import create_runtime


def duration_ms(span: ReadableSpan) -> float:
    assert span.start_time is not None and span.end_time is not None
    return (span.end_time - span.start_time) / 1e6


def memory_samples(stop, samples):  # type: ignore[no-untyped-def]
    while not stop.is_set():
        try:
            with urllib.request.urlopen("http://127.0.0.1:13200/metrics", timeout=2) as response:
                lines = response.read().decode().splitlines()
            values = {
                line.split()[0]: float(line.split()[1])
                for line in lines
                if line.startswith(
                    ("process_resident_memory_bytes ", "go_memstats_heap_alloc_bytes ")
                )
            }
            samples.append({"time": time.time(), **values})
        except Exception as error:
            samples.append({"time": time.time(), "error": type(error).__name__})
        stop.wait(0.25)


def arm(database: str, enabled: bool, index: int):  # type: ignore[no-untyped-def]
    engine = create_engine(PG.format(database), pool_size=1, max_overflow=0, pool_timeout=5)
    blocker = create_engine(PG.format(database))
    service = f"payment-feasibility-{database}-{index}"
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    capture = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(capture))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint="http://127.0.0.1:14317", insecure=True),
        )
    )
    runtime = create_runtime(service)
    runtime.tracer = provider.get_tracer("payment-phase-feasibility")
    runtime.logger.disabled = True
    observations = []
    try:
        with (
            patch.dict(
                os.environ,
                {
                    "ENABLE_TEST_FAULTS": "true",
                    "SRE_PAYMENT_PHASE_SPANS": str(enabled).lower(),
                    "FAULT_PAYMENT_DELAY_MS": "0",
                    "FAULT_PAYMENT_ERROR": "false",
                    "FAULT_PAYMENT_DB_HOLD_MS": "0",
                    "FAULT_PAYMENT_DB_QUERY_DELAY_MS": "0",
                },
            ),
            patch.object(payment, "create_runtime", return_value=runtime),
        ):
            application = payment.create_app(session_factory=sessionmaker(engine))
        with TestClient(application, raise_server_exceptions=False) as client:
            for scenario, count in (
                ("warmup", 10),
                ("normal", 100),
                ("pool", 8),
                ("application_wait", 8),
                ("sql_wait", 8),
                ("dependency_block", 8),
                ("early_error", 5),
            ):
                config = {
                    "db_hold_ms": 120 if scenario == "application_wait" else 0,
                    "db_query_delay_ms": 120 if scenario == "sql_wait" else 0,
                    "error": scenario == "early_error",
                }
                assert client.post("/__faults", json=config).status_code == 200
                for _ in range(count):
                    held = engine.connect() if scenario == "pool" else None
                    lock = blocker.connect() if scenario == "dependency_block" else None
                    if lock is not None:
                        lock.execute(text("LOCK TABLE payments IN ACCESS EXCLUSIVE MODE"))
                    result: dict[str, Any] = {}
                    before = len(capture.get_finished_spans())

                    def request(result: dict[str, Any] = result) -> None:
                        started = time.perf_counter()
                        response = client.post(
                            "/payments",
                            json={
                                "order_id": str(uuid4()),
                                "amount_cents": 100,
                                "currency": "USD",
                            },
                        )
                        result.update(
                            status=response.status_code,
                            http_ms=(time.perf_counter() - started) * 1000,
                        )

                    thread = threading.Thread(target=request)
                    try:
                        thread.start()
                        if held is not None or lock is not None:
                            time.sleep(0.12)
                    finally:
                        if held is not None:
                            held.close()
                        if lock is not None:
                            lock.rollback()
                            lock.close()
                    thread.join(timeout=10)
                    if thread.is_alive() or not result:
                        raise RuntimeError("request did not finish")
                    spans = [
                        span
                        for span in capture.get_finished_spans()[before:]
                        if span.name != "POST /__faults"
                    ]
                    durations = {span.name: duration_ms(span) for span in spans}
                    statuses = {span.name: span.status.status_code.name for span in spans}
                    server = next(span for span in spans if span.kind.name == "SERVER")
                    expected = {
                        "pool": "postgres connection acquire",
                        "application_wait": "payment transaction wait",
                        "sql_wait": "postgres payment execute",
                        "dependency_block": "postgres payment create",
                    }.get(scenario)
                    located = durations.get(expected, 0) >= 80 if expected else None
                    if scenario == "early_error":
                        located = statuses.get("payment authorize") == "ERROR" and not any(
                            name.startswith("postgres") for name in durations
                        )
                    observations.append(
                        {
                            "scenario": scenario,
                            **result,
                            "durations_ms": durations,
                            "statuses": statuses,
                            "spans": len(spans),
                            "otlp_bytes": encode_spans(spans).ByteSize(),
                            "server_ms": duration_ms(server),
                            "trace_id": f"{server.context.trace_id:032x}",
                            "located": located,
                        }
                    )
        provider.force_flush(timeout_millis=10000)
        return {"enabled": enabled, "service": service, "observations": observations}
    finally:
        provider.shutdown()
        engine.dispose()
        blocker.dispose()


def captured(service: str):  # type: ignore[no-untyped-def]
    query = urllib.parse.urlencode({"q": f'{{ resource.service.name = "{service}" }}', "limit": 8})
    with urllib.request.urlopen(
        f"http://127.0.0.1:13200/api/search?{query}", timeout=20
    ) as response:
        result = json.load(response)
    names: Counter[str] = Counter()
    traces = result.get("traces", [])[:8]
    from packages.rca.investigation.tempo import parse_tempo_trace_json

    for item in traces:
        with urllib.request.urlopen(
            f"http://127.0.0.1:13200/api/v2/traces/{item['traceID']}", timeout=20
        ) as response:
            payload = json.load(response)
        for span in parse_tempo_trace_json(payload):
            names[span.span_name or "<unnamed>"] += 1
    return {"traces_fetched": len(traces), "span_names": dict(names), "limit": 8}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    database = "payment_phase_" + uuid4().hex[:12]
    admin = create_engine(PG.format("postgres"), isolation_level="AUTOCOMMIT")
    memory: list[dict[str, Any]] = []
    stop = threading.Event()
    monitor = threading.Thread(target=memory_samples, args=(stop, memory), daemon=True)
    report: dict[str, Any] = {"database": database, "arms": [], "memory": memory, "markers": []}
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    try:
        engine = create_engine(PG.format(database))
        WorkloadBase.metadata.create_all(engine)
        engine.dispose()
        monitor.start()
        for index, enabled in enumerate((False, True, True, False)):
            report["markers"].append({"time": time.time(), "phase": "export", "arm": index})
            result = arm(database, enabled, index)
            report["arms"].append(result)
            (args.output / "observations.json").write_text(json.dumps(report, indent=2))
            print(
                f"arm {index}, enabled={enabled}, requests={len(result['observations'])}",
                flush=True,
            )
        # Export completion does not imply immediate search visibility; report bounded retries.
        report["markers"].append({"time": time.time(), "phase": "capture"})
        for result in report["arms"]:
            attempts = []
            for _ in range(3):
                attempt = captured(result["service"])
                attempts.append(attempt)
                if attempt["traces_fetched"]:
                    break
                stop.wait(2)
            result["capture_attempts"] = attempts
    finally:
        stop.set()
        if monitor.ident is not None:
            monitor.join(timeout=5)
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()
        report["temporary_database_removed"] = True
        (args.output / "observations.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
