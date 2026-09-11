"""Deterministic cross-service correlation context."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TelemetryContext:
    """Identifiers carried through HTTP, logs, and Kafka headers."""

    request_id: str
    trace_id: str
    span_id: str

    def to_headers(self) -> dict[str, str]:
        """Serialize context for transport propagation."""
        return {
            "x-request-id": self.request_id,
            "trace-id": self.trace_id,
            "span-id": self.span_id,
        }

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> "TelemetryContext":
        """Restore context and fail when a required identifier is absent."""
        required = {"request_id": "x-request-id", "trace_id": "trace-id", "span_id": "span-id"}
        missing = [name for name, header in required.items() if not headers.get(header)]
        if missing:
            raise ValueError(f"missing telemetry headers: {', '.join(missing)}")
        return cls(*(headers[header] for header in required.values()))


def structured_log(event: str, context: TelemetryContext, **fields: object) -> dict[str, object]:
    """Build a JSON-compatible structured log record."""
    return {
        "event": event,
        "level": "INFO",
        "request_id": context.request_id,
        "trace_id": context.trace_id,
        "span_id": context.span_id,
        **fields,
    }
