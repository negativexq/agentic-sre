"""m21 §36.2: an alert's rule and its Service's ExecutionRecord, read through the provider boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest
from provider_test_helpers import provider_session_factory

from packages.connector.client import (
    ConnectorAlertRuleReader,
    ConnectorClient,
    ConnectorExecutionRecordReader,
    in_process_transport,
    provider_readers,
)
from packages.connector.service import Connector
from packages.rca.investigation.prometheus import PrometheusConfig, PrometheusMetricsReader
from packages.rca.live import LokiLogReader
from packages.rca.model import Alert
from packages.rca.provider_adapter import ProviderAdapter, ProviderReaders
from packages.rca.symptom_capture import capture_symptoms, execution_capture

FIRED = datetime(2026, 10, 12, 22, 44, 44, tzinfo=UTC)
EXPRESSION = 'sum(rate(http_requests_total{service="order-service",status=~"5.."}[30s])) > 0.05'
LABELS = {"namespace": "sre-demo", "service": "order-service", "severity": "critical"}


class _Response:
    def __init__(self, body: object) -> None:
        self.body = json.dumps(body).encode()
        self.status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.body


class _Opener:
    def __init__(self, handler: Callable[[str, dict[str, list[str]]], object]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, list[str]]]] = []

    def __call__(self, request: Request, *, timeout: float) -> _Response:
        parts = urlsplit(request.full_url)
        params = parse_qs(parts.query)
        self.calls.append((parts.path, params))
        return _Response(self.handler(parts.path, params))


def _rule(name: str, labels: Mapping[str, str], query: str = EXPRESSION) -> dict[str, object]:
    return {
        "name": name,
        "query": query,
        "duration": 15,
        "labels": dict(labels),
        "type": "alerting",
    }


def _prometheus(
    rules: list[dict[str, object]],
    series: list[dict[str, str]] | None = None,
    targets: list[dict[str, object]] | None = None,
) -> tuple[PrometheusMetricsReader, _Opener]:
    selected = [{"job": "order-service", "instance": "order:8000"}] if series is None else series
    active = (
        [{"labels": {"job": "order-service", "instance": "order:8000"}, "scrapeInterval": "5s"}]
        if targets is None
        else targets
    )

    def handler(path: str, params: dict[str, list[str]]) -> object:
        if path.endswith("/rules"):
            groups = [{"name": "live", "interval": 5, "rules": rules}]
            return {"status": "success", "data": {"groups": groups}}
        if path.endswith("/query") and "[" in params["query"][0]:
            name = params["query"][0].split("{", 1)[0]
            matrix = [
                {
                    "metric": dict(labels, __name__=name),
                    "values": [[1000.5, "3" if name == "http_requests_total" else "0.01"]],
                }
                for labels in selected
            ]
            return {"status": "success", "data": {"resultType": "matrix", "result": matrix}}
        if path.endswith("/query"):
            result = [{"metric": labels, "value": [0, "1"]} for labels in selected]
            return {"status": "success", "data": {"resultType": "vector", "result": result}}
        return {"status": "success", "data": {"activeTargets": active}}

    opener = _Opener(handler)
    return PrometheusMetricsReader(PrometheusConfig("http://prometheus"), opener=opener), opener


def test_the_alert_rule_its_group_interval_and_the_selected_targets_scrape_interval() -> None:
    reader, opener = _prometheus([_rule("OrderErrorRateHigh", LABELS)])
    found = reader.alert_rule("OrderErrorRateHigh", {**LABELS, "alertname": "x"}, FIRED)
    assert found == {
        "expression": EXPRESSION,
        "for_seconds": 15.0,
        "interval_seconds": 5.0,
        "scrape_interval_seconds": 5.0,
        "counter_series": [
            {
                "labels": {"job": "order-service", "instance": "order:8000"},
                "samples": [[1000.5, 3.0]],
            }
        ],
        "scrape_durations": [
            {
                "labels": {"job": "order-service", "instance": "order:8000"},
                "samples": [[1000.5, 0.01]],
            }
        ],
    }
    queries = [params for path, params in opener.calls if path.endswith("/query")]
    assert [q["query"][0] for q in queries] == [
        'group by (job, instance) (http_requests_total{service="order-service",status=~"5.."})',
        # m21 §36.6: the counter's samples over (firing - for - R, firing] and its target's scrape durations
        'http_requests_total{service="order-service",status=~"5.."}[45s]',
        'scrape_duration_seconds{job="order-service",instance="order:8000"}[45s]',
    ]
    # the samples are read at the firing itself, to the millisecond
    assert [q["time"][0] for q in queries[1:]] == [str(FIRED.timestamp())] * 2


def test_an_ambiguous_or_missing_rule_has_no_expression() -> None:
    both = [
        _rule("OrderErrorRateHigh", LABELS),
        _rule("OrderErrorRateHigh", {"severity": "critical"}),
    ]
    reader, _ = _prometheus(both)
    assert reader.alert_rule("OrderErrorRateHigh", LABELS, FIRED) == {
        "expression": None,
        "rules_matched": 2,
    }
    other = {**LABELS, "service": "payment-service"}
    reader, _ = _prometheus([_rule("OrderErrorRateHigh", other)])
    assert reader.alert_rule("OrderErrorRateHigh", LABELS, FIRED)["expression"] is None


def test_the_scrape_interval_is_unknown_unless_every_selected_target_is_listed() -> None:
    reader, _ = _prometheus([_rule("OrderErrorRateHigh", LABELS)], targets=[])
    assert reader.alert_rule("OrderErrorRateHigh", LABELS, FIRED)["scrape_interval_seconds"] is None
    reader, _ = _prometheus([_rule("OrderErrorRateHigh", LABELS)], series=[])
    assert reader.alert_rule("OrderErrorRateHigh", LABELS, FIRED)["scrape_interval_seconds"] is None
    slow: list[dict[str, object]] = [
        {"labels": {"job": "order-service", "instance": "a"}, "scrapeInterval": "5s"},
        {"labels": {"job": "order-service", "instance": "b"}, "scrapeInterval": "1m30s"},
    ]
    two = [{"job": "order-service", "instance": "a"}, {"job": "order-service", "instance": "b"}]
    reader, _ = _prometheus([_rule("OrderErrorRateHigh", LABELS)], series=two, targets=slow)
    assert reader.alert_rule("OrderErrorRateHigh", LABELS, FIRED)["scrape_interval_seconds"] == 90.0


def _entry(at_ns: int, record: str, **fields: str) -> tuple[int, dict[str, str]]:
    return at_ns, {
        "service_name": "order-service",
        "execution_record": record,
        "trace_id": "t1",
        "request_id": "dropped",
        **fields,
    }


def test_execution_records_are_paged_forward_and_keep_only_the_fields_the_rule_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("packages.rca.live.EXECUTION_RECORD_PAGE", 2)
    base = int(FIRED.timestamp() * 1e9)
    entries = [
        _entry(base, "start"),
        _entry(
            base + 1,
            "operation",
            server_address="payment-service",
            error_type="ConnectionRefusedError",
        ),
        _entry(base + 2, "terminal", http_response_status_code="503"),
    ]

    def handler(path: str, params: dict[str, list[str]]) -> object:
        start = int(params["start"][0])
        page = [(ts, labels) for ts, labels in entries if ts >= start][:2]
        result = [{"stream": labels, "values": [[str(ts), "execution.x"]]} for ts, labels in page]
        return {"status": "success", "data": {"result": result}}

    opener = _Opener(handler)
    reader = LokiLogReader("http://loki", opener=opener)
    found = reader.execution_records(
        "sre-demo", "order-service", FIRED, FIRED + timedelta(seconds=1)
    )
    assert [e["execution_record"] for e in found] == ["start", "operation", "terminal"]
    assert all("request_id" not in e and "service_name" not in e for e in found)
    assert found[2] == {
        "execution_record": "terminal",
        "trace_id": "t1",
        "http_response_status_code": "503",
        "timestamp_ns": str(base + 2),
    }
    assert opener.calls[0][1]["query"] == [
        '{service_name="order-service", k8s_namespace_name="sre-demo"} |= "execution."'
    ]
    # each page starts again at its last nanosecond; the duplicate is dropped
    assert [p["start"] for _, p in opener.calls] == [[str(base)], [str(base + 1)], [str(base + 2)]]


def test_a_window_too_full_to_read_whole_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("packages.rca.live.EXECUTION_RECORD_MAX_ENTRIES", 1)
    base = int(FIRED.timestamp() * 1e9)

    def handler(path: str, params: dict[str, list[str]]) -> object:
        result = [
            {"stream": _entry(base + i, "start")[1], "values": [[str(base + i), "x"]]}
            for i in range(2)
        ]
        return {"status": "success", "data": {"result": result}}

    reader = LokiLogReader("http://loki", opener=_Opener(handler))
    with pytest.raises(ValueError, match="more than 1"):
        reader.execution_records("sre-demo", "order-service", FIRED, FIRED + timedelta(seconds=1))


def test_execution_capture_reads_terminals_and_operations() -> None:
    base = int(FIRED.timestamp() * 1e9)
    capture = execution_capture(
        "provider-read:7",
        "sre-demo",
        "order-service",
        FIRED - timedelta(seconds=45),
        FIRED,
        [
            {"execution_record": "start", "trace_id": "t1", "timestamp_ns": str(base)},
            {
                "execution_record": "operation",
                "trace_id": "t1",
                "execution_operation_kind": "HTTP_CLIENT",
                "execution_operation_outcome": "ERROR",
                "execution_operation_started_ns": str(base + 1000),
                "execution_operation_duration_ns": "2000000",
                "server_address": "payment-service",
                "error_type": "ConnectionRefusedError",
            },
            {
                "execution_record": "terminal",
                "trace_id": "t1",
                "http_response_status_code": "503",
                "k8s_pod_uid": "pod-1",
                "timestamp_ns": str(base + 5000),
            },
            {"execution_record": "terminal", "timestamp_ns": str(base)},
        ],
    )
    assert [(t.trace_id, t.status, t.pod_uid) for t in capture.terminals] == [("t1", 503, "pod-1")]
    (operation,) = capture.operations
    assert (operation.kind, operation.outcome, operation.server_address, operation.error_type) == (
        "HTTP_CLIENT",
        "ERROR",
        "payment-service",
        "ConnectionRefusedError",
    )
    assert operation.duration_seconds == 0.002


class _Rules:
    def __init__(self, found: dict[str, object] | Exception) -> None:
        self.found = found
        self.calls: list[tuple[str, dict[str, str], datetime]] = []

    def alert_rule(self, alert: str, labels: Mapping[str, str], at: datetime) -> dict[str, object]:
        self.calls.append((alert, dict(labels), at))
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


class _Records:
    def __init__(self, entries: list[dict[str, str]] | Exception) -> None:
        self.entries = entries
        self.windows: list[tuple[str, str, datetime, datetime]] = []

    def execution_records(
        self, namespace: str, service: str, starts_at: datetime, ends_at: datetime
    ) -> list[dict[str, str]]:
        self.windows.append((namespace, service, starts_at, ends_at))
        if isinstance(self.entries, Exception):
            raise self.entries
        return self.entries


RULE = {
    "expression": EXPRESSION,
    "for_seconds": 15.0,
    "interval_seconds": 5.0,
    "scrape_interval_seconds": 5.0,
}
ALERT = Alert(
    name="OrderErrorRateHigh",
    service="order-service",
    namespace="sre-demo",
    starts_at=FIRED,
    labels={**LABELS, "service_name": "order-service"},
)


def _adapter(prometheus: object, loki: object) -> ProviderAdapter:
    return ProviderAdapter(
        "test-run",
        "CAPTURE",
        provider_session_factory(),
        ProviderReaders(prometheus=prometheus, loki=loki),  # type: ignore[arg-type]
    )


def test_a_rule_of_the_form_is_captured_with_its_execution_record_window() -> None:
    rules, records = _Rules(RULE), _Records([])
    (capture,) = capture_symptoms(_adapter(rules, records), [ALERT, ALERT])
    assert rules.calls == [("OrderErrorRateHigh", LABELS, FIRED)]
    assert capture.rule.expression == EXPRESSION and not capture.rule.failed
    assert capture.rule.evidence_id.startswith("provider-read:")
    # from the pending start minus the expression's range to the firing
    assert records.windows == [("sre-demo", "order-service", FIRED - timedelta(seconds=45), FIRED)]
    assert capture.execution is not None and not capture.execution.failed
    assert capture.execution.evidence_id.startswith("provider-read:")


def test_another_form_reads_no_execution_record() -> None:
    records = _Records([])
    ratio = {**RULE, "expression": "sum(rate(a[30s])) / sum(rate(b[30s])) > 0.5"}
    (capture,) = capture_symptoms(_adapter(_Rules(ratio), records), [ALERT])
    assert capture.execution is None and records.windows == []


def test_failed_reads_are_captured_as_failed() -> None:
    (capture,) = capture_symptoms(_adapter(_Rules(TimeoutError("down")), _Records([])), [ALERT])
    assert capture.rule.failed and capture.execution is None
    (capture,) = capture_symptoms(_adapter(_Rules(RULE), _Records(TimeoutError("down"))), [ALERT])
    assert not capture.rule.failed
    assert capture.execution is not None and capture.execution.failed


def test_nothing_is_captured_where_the_providers_serve_neither_read() -> None:
    class Plain:
        pass

    assert capture_symptoms(_adapter(Plain(), _Records([])), [ALERT]) == ()
    assert capture_symptoms(_adapter(_Rules(RULE), Plain()), [ALERT]) == ()
    unnamed = ALERT.model_copy(update={"service": None})
    assert capture_symptoms(_adapter(_Rules(RULE), _Records([])), [unnamed]) == ()


def test_the_connector_serves_both_reads_and_reports_them() -> None:
    rules, records = _Rules(RULE), _Records([{"execution_record": "start", "trace_id": "t1"}])

    class Prometheus(_Rules):
        def query_traffic(self, *args: Any) -> tuple[()]:
            return ()

        def query_resource_pressure(self, *args: Any) -> tuple[()]:
            return ()

    class Loki(_Records):
        def error_logs(self, *args: Any, **kwargs: Any) -> list[object]:
            return []

    prometheus, loki = Prometheus(RULE), Loki(records.entries)
    connector = Connector(providers=ProviderReaders(prometheus=prometheus, loki=loki))  # type: ignore[arg-type]
    client = ConnectorClient(in_process_transport(connector))
    assert {"alert_rules", "execution_records"} <= set(client.capabilities())
    readers = provider_readers(client)
    assert isinstance(readers.prometheus, ConnectorAlertRuleReader)
    assert isinstance(readers.loki, ConnectorExecutionRecordReader)
    assert readers.prometheus.alert_rule("OrderErrorRateHigh", LABELS, FIRED) == RULE
    assert prometheus.calls == [("OrderErrorRateHigh", LABELS, FIRED)]
    assert (
        readers.loki.execution_records("sre-demo", "order-service", FIRED, FIRED) == records.entries
    )
    del rules


def test_a_connector_without_the_reads_gives_readers_without_them() -> None:
    class Loki:
        def error_logs(self, *args: Any, **kwargs: Any) -> list[object]:
            return []

    connector = Connector(providers=ProviderReaders(loki=Loki()))  # type: ignore[arg-type]
    readers = provider_readers(ConnectorClient(in_process_transport(connector)))
    assert not isinstance(readers.loki, ConnectorExecutionRecordReader)
    adapter = _adapter(readers.prometheus, readers.loki)
    assert not adapter.supports_symptom_capture()
