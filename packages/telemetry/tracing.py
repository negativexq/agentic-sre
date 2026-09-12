"""OpenTelemetry tracer construction for workload services."""

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def create_tracer(service_name: str, *, otlp_endpoint: str | None = None) -> trace.Tracer:
    """Create a service-scoped tracer, optionally exporting to the Collector."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if otlp_endpoint is not None:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    return provider.get_tracer("agentic-sre", "0.1.1")
