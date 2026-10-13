"""Local HTTP DEV measurement; no Kubernetes, shared collector, or RCA evaluation.

Real loopback HTTP faults, SDK spans, structured logs and an independent driver
ledger. In-memory always-on capture is not production sampling/transport coverage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import uvicorn
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import CollectorRegistry
from starlette.types import Receive, Scope, Send
from workload.common.adapters import HttpPaymentGateway
from workload.common.contracts import PaymentRequest

from packages.evals.frozen_request_binding import (
    FrozenLog,
    FrozenRequestEvidence,
    FrozenSpan,
    corroborate_frozen_requests,
)
from packages.rca.log_attributes import source_log_attributes
from packages.rca.model import LogRecord, TraceSpanObservation, TraceSpanStatus
from packages.rca.traces import semantic_attributes
from packages.telemetry import create_runtime
from packages.telemetry.runtime import JsonLogFormatter, TelemetryMiddleware


class Capture(logging.Handler):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(JsonLogFormatter(service))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def measure(
    output: Path, repeats: int, workers: int, *, otlp_endpoint: str | None = None
) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Use a fresh output directory; measurements are not overwritten")
    output.mkdir(parents=True)
    scenarios = ("refusal", "timeout", "http503", "handled-http503", "success")
    expected_types = {
        "refusal": "ConnectionRefusedError",
        "timeout": "TimeoutError",
        "http503": "503",
        "handled-http503": "503",
    }
    protocol = {
        "profile": "LOCAL_HTTP_DEV_CAPTURE",
        "rca_evaluated": False,
        "otlp_endpoint": otlp_endpoint,
        "scenarios": scenarios,
        "requests_per_scenario": repeats,
        "workers": workers,
        "expected_dependency_error_types": expected_types,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "working_tree_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True)
        ),
        "capture": "IN_MEMORY_ALWAYS_ON_SDK_AND_JSON_LOGS",
        "acceptance": {
            "positive_binding_coverage": 1.0,
            "false_corroborated_controls": 0,
            "driver_errors": 0,
        },
        "non_claims": [
            "NO_KUBERNETES_HOLDOUT",
            "NO_RCA_OR_RESOLVED_SCORE",
            "NO_PRODUCTION_TRANSPORT_OR_SAMPLING_COVERAGE",
            "NO_SAME_FAULT_OR_RECOVERY_AUTHORITY",
        ],
    }
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    (output / "runner.py").write_bytes(Path(__file__).read_bytes())
    run_id = f"local-dev-{uuid4().hex}"
    service = f"binding-dev-order-{uuid4().hex[:8]}"
    runtime = create_runtime(service, registry=CollectorRegistry())
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime.tracer = provider.get_tracer("local-binding-dev")
    runtime.execution_logger = None
    capture = Capture(service)
    runtime.logger.handlers = [capture]
    log_provider = None
    if otlp_endpoint is not None:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
        )
        log_provider = LoggerProvider(resource=Resource.create({"service.name": service}))
        log_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=otlp_endpoint, insecure=True))
        )
        runtime.logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=log_provider))

    class Payment(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path.startswith("/timeout"):
                time.sleep(0.25)
            if self.path.startswith("/http503"):
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(201)
            self.end_headers()
            try:
                self.wfile.write(
                    json.dumps(
                        {
                            "payment_id": str(uuid4()),
                            "order_id": body["order_id"],
                            "status": "APPROVED",
                        }
                    ).encode()
                )
            except (BrokenPipeError, ConnectionResetError):
                pass  # expected after the caller's timeout

        def log_message(self, format: str, *args: object) -> None:
            pass

    payment = ThreadingHTTPServer(("127.0.0.1", 0), Payment)
    payment.daemon_threads = True
    payment_thread = threading.Thread(target=payment.serve_forever, daemon=True)
    payment_thread.start()
    refusal = socket.socket()
    refusal.bind(("127.0.0.1", 0))
    refusal_port = refusal.getsockname()[1]
    refusal.close()  # an unbound loopback port refuses; bound nonlisteners can time out on macOS
    host = f"http://127.0.0.1:{payment.server_port}"
    gateways = {
        "refusal": HttpPaymentGateway(f"http://127.0.0.1:{refusal_port}", runtime=runtime),
        "timeout": HttpPaymentGateway(host + "/timeout", timeout_seconds=0.05, runtime=runtime),
        "http503": HttpPaymentGateway(host + "/http503", runtime=runtime),
        "handled-http503": HttpPaymentGateway(host + "/http503", runtime=runtime),
        "success": HttpPaymentGateway(host, runtime=runtime),
    }

    async def order(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        scenario = scope["path"].lstrip("/")
        status = 201
        try:
            await asyncio.to_thread(
                gateways[scenario].charge,
                PaymentRequest(order_id=uuid4(), amount_cents=100, currency="USD"),
            )
        except (URLError, TimeoutError):
            status = 201 if scenario == "handled-http503" else 500
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(TelemetryMiddleware(order, runtime), log_level="critical", lifespan="off")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    ledger: list[dict[str, Any]] = []
    start = datetime.now(UTC)
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local DEV HTTP server failed to start")
            time.sleep(0.01)

        def drive(scenario: str) -> dict[str, Any]:
            request_id = uuid4().hex
            begun = datetime.now(UTC)
            try:
                with urlopen(
                    Request(
                        f"http://127.0.0.1:{port}/{scenario}", headers={"x-request-id": request_id}
                    ),
                    timeout=10,
                ) as response:
                    status = response.status
            except HTTPError as error:
                status = error.code
            return {
                "scenario": scenario,
                "request_id": request_id,
                "start": begun.isoformat(),
                "end": datetime.now(UTC).isoformat(),
                "status": status,
            }

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for scenario in scenarios:
                ledger.extend(pool.map(drive, [scenario] * repeats))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        refusal.close()
        payment.shutdown()
        payment.server_close()
        payment_thread.join(timeout=2)
    if not provider.force_flush(timeout_millis=10000):
        raise RuntimeError("DEV trace exporter did not flush")
    if log_provider is not None and not log_provider.force_flush(timeout_millis=10000):
        raise RuntimeError("DEV log exporter did not flush")
    captured = datetime.now(UTC)
    end = captured
    provenance: dict[str, Any] = {"incident_id": run_id, "run_id": run_id, "captured_at": captured}
    logs = []
    for index, line in enumerate(capture.lines):
        body = json.loads(line)
        logs.append(
            FrozenLog(
                **provenance,
                source_reference=f"local-json:{index}",
                record=LogRecord(
                    service=body["service"],
                    at=datetime.fromisoformat(body["timestamp"]),
                    severity=body["level"],
                    message=line[:300],
                    evidence_id=f"local-log:{index}",
                    attributes=source_log_attributes(line),
                ),
            )
        )
    spans = []
    for sdk in exporter.get_finished_spans():
        assert sdk.start_time is not None and sdk.end_time is not None
        spans.append(
            FrozenSpan(
                **provenance,
                source_reference=f"local-sdk:{sdk.context.span_id:016x}",
                record=TraceSpanObservation(
                    trace_id=f"{sdk.context.trace_id:032x}",
                    span_id=f"{sdk.context.span_id:016x}",
                    parent_span_id=f"{sdk.parent.span_id:016x}" if sdk.parent else None,
                    service=str(sdk.resource.attributes["service.name"]),
                    span_kind=sdk.kind.name,
                    start_at=datetime.fromtimestamp(sdk.start_time / 1e9, UTC),
                    end_at=datetime.fromtimestamp(sdk.end_time / 1e9, UTC),
                    status=TraceSpanStatus.ERROR
                    if sdk.status.status_code.name == "ERROR"
                    else TraceSpanStatus.UNKNOWN,
                    semantic_attributes=semantic_attributes(
                        {"SpanAttributes": sdk.attributes or {}}
                    ),
                    evidence_id=f"local-span:{sdk.context.span_id:016x}",
                ),
            )
        )
    evidence = FrozenRequestEvidence(
        incident_id=run_id,
        run_id=run_id,
        episode_start=start,
        episode_end=end,
        frozen_cutoff=captured,
        caller_service=service,
        configured_peer="127.0.0.1",
        logs=tuple(logs),
        spans=tuple(spans),
    )
    report = corroborate_frozen_requests(evidence)
    results = {row.log_evidence_id: row for row in report.relations}
    by_request: dict[str, list[FrozenLog]] = {}
    for item in logs:
        if "error.type" in item.record.attributes:
            by_request.setdefault(item.record.attributes.get("request_id", ""), []).append(item)
    counts: dict[str, Any] = {}
    for scenario in scenarios:
        requests = [row for row in ledger if row["scenario"] == scenario]
        attempts = [log for row in requests for log in by_request.get(row["request_id"], [])]
        expected_positive = scenario in {"refusal", "timeout", "http503"}
        expected_status = 500 if expected_positive else 201
        counts[scenario] = {
            "driver_requests": len(requests),
            "unexpected_http_status_count": sum(
                row["status"] != expected_status for row in requests
            ),
            "dependency_error_logs": len(attempts),
            "requests_with_dependency_error_log": sum(
                bool(by_request.get(row["request_id"])) for row in requests
            ),
            "corroborated": sum(
                results[log.record.evidence_id].request_relation == "CORROBORATED"
                for log in attempts
            ),
            "unassessed": sum(
                results[log.record.evidence_id].request_relation == "UNASSESSED" for log in attempts
            ),
            "error_types": dict(
                Counter(log.record.attributes.get("error.type") for log in attempts)
            ),
            "deficits": dict(
                Counter(
                    code for log in attempts for code in results[log.record.evidence_id].deficits
                )
            ),
            "expected_positive_relation": expected_positive,
            "unexpected_dependency_error_type_count": sum(
                log.record.attributes.get("error.type") != expected_types.get(scenario)
                for log in attempts
            ),
        }
    # Controlled ablations use copies of captured facts and never overwrite the base export.
    positive_requests = {
        row["request_id"] for row in ledger if row["scenario"] in {"refusal", "timeout", "http503"}
    }
    positives = [
        item
        for item in logs
        if item.record.attributes.get("request_id") in positive_requests
        and "error.type" in item.record.attributes
    ]
    controls: dict[str, Any] = {}
    for control in (
        "missing_parent",
        "wrong_request",
        "duplicate_client",
        "wrong_peer",
        "different_trace_decoy",
        "capture_after_cutoff",
    ):
        changed = []
        for item in positives:
            attrs = item.record.attributes
            pair = [s for s in spans if s.record.trace_id == attrs["trace_id"]]
            altered = []
            for span in pair:
                if control == "missing_parent" and span.record.span_id == attrs["parent_span_id"]:
                    continue
                updates: dict[str, Any] = {}
                if control == "wrong_request":
                    updates["semantic_attributes"] = {
                        **span.record.semantic_attributes,
                        "http.request.header.x_request_id": "other-request",
                    }
                if control == "wrong_peer" and span.record.span_kind == "CLIENT":
                    updates["semantic_attributes"] = {
                        **span.record.semantic_attributes,
                        "server.address": "other-peer",
                    }
                if control == "different_trace_decoy":
                    updates["trace_id"] = "f" * 32
                altered.append(
                    span.model_copy(update={"record": span.record.model_copy(update=updates)})
                )
            if control == "duplicate_client":
                altered.extend(s for s in pair if s.record.span_kind == "CLIENT")
            modified = (
                item.model_copy(update={"captured_at": captured + timedelta(seconds=1)})
                if control == "capture_after_cutoff"
                else item
            )
            assessment = corroborate_frozen_requests(
                evidence.model_copy(update={"logs": (modified,), "spans": tuple(altered)})
            )
            changed.append(assessment.relations[0])
        controls[control] = {
            "cases": len(changed),
            "false_corroborated": sum(row.request_relation == "CORROBORATED" for row in changed),
            "deficits": dict(Counter(code for row in changed for code in row.deficits)),
        }
    denominator = sum(counts[s]["driver_requests"] for s in ("refusal", "timeout", "http503"))
    numerator = sum(counts[s]["corroborated"] for s in ("refusal", "timeout", "http503"))
    summary = {
        "profile": protocol["profile"],
        "scenario_validation_passed": all(
            row["unexpected_http_status_count"] == 0
            and row["unexpected_dependency_error_type_count"] == 0
            and row["dependency_error_logs"] == (0 if scenario == "success" else repeats)
            for scenario, row in counts.items()
        ),
        "run_id": run_id,
        "commit": protocol["commit"],
        "capture": protocol["capture"],
        "rca_evaluated": False,
        "scenarios": counts,
        "controls": controls,
        "positive_binding_coverage": {
            "corroborated": numerator,
            "expected_failed_requests": denominator,
            "ratio": numerator / denominator,
        },
        "false_corroborated_controls": sum(row["false_corroborated"] for row in controls.values()),
        "manifest_membership": "NOT_VERIFIED_LOCAL_FILE_CAPTURE",
        "source_authenticity": "NOT_VERIFIED",
    }
    (output / "driver-ledger.json").write_text(json.dumps(ledger, indent=2) + "\n")
    (output / "frozen-evidence.json").write_text(evidence.model_dump_json(indent=2) + "\n")
    (output / "binding-report.json").write_text(report.model_dump_json(indent=2) + "\n")
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    provider.shutdown()
    if log_provider is not None:
        log_provider.shutdown()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 1000 or not 1 <= args.workers <= 32:
        parser.error("repeats 1..1000; workers 1..32 required")
    result = measure(args.output, args.repeats, args.workers)
    print(json.dumps(result, indent=2))
    if (
        not result["scenario_validation_passed"]
        or result["positive_binding_coverage"]["ratio"] != 1.0
        or result["false_corroborated_controls"]
        or result["scenarios"]["handled-http503"]["corroborated"]
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
