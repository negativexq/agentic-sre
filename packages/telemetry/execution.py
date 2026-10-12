"""ExecutionRecord: the start, operations and terminal of every request (symptom explanation design §4.2).

The records go to a logger of their own, at INFO, so they never mix with the error logs the engine reads. A start
without a terminal is a lifecycle gap, never a success; operations are the request's finished outgoing calls, each
recorded inside its own span's context.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import context, trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanKind, StatusCode

POD_UID = os.getenv("POD_UID") or None


def operation_kind(kind: SpanKind | None, attributes: Any, name: str) -> str | None:
    """The ExecutionRecord operation kind of a finished span, or None for spans that are not outgoing calls."""
    if kind not in {SpanKind.CLIENT, SpanKind.PRODUCER}:
        return None
    if attributes.get("db.system"):
        return "DB"
    if kind is SpanKind.PRODUCER or attributes.get("messaging.system"):
        return "MESSAGING_PUBLISH"
    if attributes.get("http.request.method"):
        return "HTTP_CLIENT"
    return "OTHER"


class ExecutionRecordProcessor(SpanProcessor):
    """Writes one operation record for every finished outgoing call, in that call's span context."""

    def __init__(self, logger: logging.Logger, pod_uid: str | None = POD_UID) -> None:
        self._logger = logger
        self._pod_uid = pod_uid

    def on_end(self, span: ReadableSpan) -> None:
        attributes = span.attributes or {}
        kind = operation_kind(span.kind, attributes, span.name)
        if kind is None or span.context is None:
            return
        failed = span.status.status_code is StatusCode.ERROR or "error.type" in attributes
        token = context.attach(trace.set_span_in_context(NonRecordingSpan(span.context)))
        try:
            self._logger.info(
                "execution.operation",
                extra={
                    "execution.record": "operation",
                    "execution.operation.kind": kind,
                    "execution.operation.name": span.name,
                    "execution.operation.started_ns": span.start_time,
                    "execution.operation.duration_ns": (span.end_time or 0)
                    - (span.start_time or 0),
                    "execution.operation.outcome": "ERROR" if failed else "OK",
                    "server.address": attributes.get("server.address"),
                    "error.type": attributes.get("error.type"),
                    "k8s.pod.uid": self._pod_uid,
                },
            )
        finally:
            context.detach(token)


def record_start(logger: logging.Logger | None, method: str, path: str, request_id: str) -> None:
    if logger is not None:
        logger.info(
            "execution.start",
            extra={
                "execution.record": "start",
                "http.request.method": method,
                "url.path": path,
                "request_id": request_id,
                "k8s.pod.uid": POD_UID,
            },
        )


def record_terminal(
    logger: logging.Logger | None, method: str, path: str, request_id: str, status: int
) -> None:
    if logger is not None:
        logger.info(
            "execution.terminal",
            extra={
                "execution.record": "terminal",
                "http.request.method": method,
                "url.path": path,
                "request_id": request_id,
                "http.response.status_code": status,
                "k8s.pod.uid": POD_UID,
            },
        )
