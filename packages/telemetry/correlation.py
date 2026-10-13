"""Request-local correlation, reset on exit and never synthesized for a dependency."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry import trace

_REQUEST_ID: ContextVar[str | None] = ContextVar("telemetry_request_id", default=None)


@contextmanager
def request_context(request_id: str) -> Iterator[None]:
    token = _REQUEST_ID.set(request_id)
    try:
        yield
    finally:
        _REQUEST_ID.reset(token)


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


def span_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return format(context.span_id, "016x") if context.is_valid else None


def correlation_fields(parent_span_id: str | None) -> dict[str, str | None]:
    context = trace.get_current_span().get_span_context()
    return {
        "request_id": current_request_id(),
        "trace_id": format(context.trace_id, "032x") if context.is_valid else None,
        "span_id": format(context.span_id, "016x") if context.is_valid else None,
        "parent_span_id": parent_span_id,
    }
