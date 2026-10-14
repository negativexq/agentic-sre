"""Request-pair observations retain exact typed shape on PostgreSQL tape."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from packages.rca.investigation.tempo import TempoTraceBatch
from packages.rca.model import (
    EntityRef,
    InvestigationQuery,
    LogRecord,
    TraceSpanObservation,
    TraceSpanStatus,
)
from packages.rca.provider_adapter import ProviderAdapter, ProviderReaders
from packages.rca.replay import ReplayProviderAdapter, _Cursor, _load_tape
from packages.rca.trace_request_pairs import RequestPairBatch, read_request_pairs
from packages.storage.models import Base

pytestmark = pytest.mark.postgres


def test_postgres_pair_result_and_source_hash_replay(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    at = datetime(2026, 10, 14, tzinfo=UTC)
    target = EntityRef(namespace="dev", kind="Deployment", name="order")
    tid, cid, pid = "a" * 32, "b" * 16, "c" * 16
    attrs = {
        "request_id": "req",
        "trace_id": tid,
        "span_id": cid,
        "parent_span_id": pid,
        "server.address": "payment",
        "error.type": "ConnectionRefusedError",
        "http.request.method": "POST",
    }
    log = LogRecord(
        service="order",
        at=at + timedelta(seconds=2),
        severity="ERROR",
        message="dependency refused",
        evidence_id="log:1",
        attributes=attrs,
    )
    resource = {
        "k8s.namespace.name": "dev",
        "k8s.deployment.name": "order",
        "http.request.header.x_request_id": "req",
    }
    spans = (
        TraceSpanObservation(
            trace_id=tid,
            span_id=cid,
            parent_span_id=pid,
            service="order",
            span_kind="CLIENT",
            start_at=at + timedelta(seconds=1),
            end_at=at + timedelta(seconds=3),
            status=TraceSpanStatus.ERROR,
            evidence_id="client",
            semantic_attributes={
                **resource,
                "server.address": "payment",
                "error.type": "ConnectionRefusedError",
                "http.request.method": "POST",
            },
        ),
        TraceSpanObservation(
            trace_id=tid,
            span_id=pid,
            service="order",
            span_kind="SERVER",
            start_at=at,
            end_at=at + timedelta(seconds=4),
            evidence_id="parent",
            semantic_attributes={**resource, "http.response.status_code": "500"},
        ),
    )

    class Reader:
        def query(self, target: EntityRef, query: InvestigationQuery) -> TempoTraceBatch:
            raise AssertionError("General search forbidden")

        def query_pairs(
            self,
            target: EntityRef,
            query: InvestigationQuery,
            logs: Sequence[LogRecord],
            existing: Sequence[TraceSpanObservation],
        ) -> RequestPairBatch:
            return read_request_pairs(target, query, logs, existing, lambda tid: spans)

    adapter = ProviderAdapter("pairs-pg", "INVESTIGATION", factory, ProviderReaders(tempo=Reader()))
    query = InvestigationQuery(start=at, end=at + timedelta(seconds=5), source_log_ids=("log:1",))
    live = adapter.query_tempo_pairs(target, query, (log,), ())
    replay = ReplayProviderAdapter(
        "pairs-pg",
        "INVESTIGATION",
        adapter.capabilities(),
        _load_tape(factory, "pairs-pg"),
        _Cursor(0),
    )
    assert replay.query_tempo_pairs(target, query, (log,), ()) == live
    assert replay.next_sequence is None and isinstance(live, RequestPairBatch)
    assert live.assessments[0].state == "CORROBORATED"
    engine.dispose()
