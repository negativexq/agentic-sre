"""m21 §36.2: a firing alert's rule and its Service's ExecutionRecord, captured before the run's manifest.

Both reads go through the provider boundary as CAPTURE reads, so they are taped; the run's boundary event freezes
what they returned, and the engine and replay read only that.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from packages.rca.model import (
    Alert,
    AlertRuleCapture,
    ExecutionOperation,
    ExecutionRecordCapture,
    ExecutionTerminal,
    ProviderReadFailure,
    SymptomCapture,
)
from packages.rca.provider_adapter import ProviderAdapter
from packages.rca.refusal_sufficiency import recognize


def _at(nanoseconds: str | None) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(nanoseconds or "") / 1e9, tz=UTC)
    except ValueError:
        return None


def execution_capture(
    evidence_id: str,
    namespace: str,
    service: str,
    starts_at: datetime,
    ends_at: datetime,
    entries: Iterable[Mapping[str, str]],
) -> ExecutionRecordCapture:
    """The terminal and operation records of the entries; a start record carries nothing the rule reads."""
    terminals: list[ExecutionTerminal] = []
    operations: list[ExecutionOperation] = []
    for entry in entries:
        trace_id, record = entry.get("trace_id"), entry.get("execution_record")
        if not trace_id:
            continue
        if record == "terminal" and entry.get("http_response_status_code"):
            at = _at(entry.get("timestamp_ns"))
            if at is not None:
                terminals.append(
                    ExecutionTerminal(
                        trace_id=trace_id,
                        at=at,
                        status=int(entry["http_response_status_code"]),
                        pod_uid=entry.get("k8s_pod_uid") or None,
                    )
                )
        elif record == "operation":
            started = _at(entry.get("execution_operation_started_ns"))
            if started is not None:
                operations.append(
                    ExecutionOperation(
                        trace_id=trace_id,
                        started_at=started,
                        duration_seconds=int(entry.get("execution_operation_duration_ns") or 0)
                        / 1e9,
                        kind=entry.get("execution_operation_kind") or "OTHER",
                        outcome=entry.get("execution_operation_outcome") or "OK",
                        server_address=entry.get("server_address") or None,
                        error_type=entry.get("error_type") or None,
                    )
                )
    return ExecutionRecordCapture(
        evidence_id=evidence_id,
        namespace=namespace,
        service=service,
        starts_at=starts_at,
        ends_at=ends_at,
        terminals=tuple(sorted(terminals, key=lambda t: (t.at, t.trace_id))),
        operations=tuple(sorted(operations, key=lambda o: (o.started_at, o.trace_id))),
    )


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def capture_symptom(adapter: ProviderAdapter, alert: Alert) -> SymptomCapture | None:
    """One firing alert's rule and, for a rule of §36's form, its Service's ExecutionRecord from the pending start
    minus the expression's range to the firing. None for an alert that names no Service."""
    if not alert.service or not alert.namespace:
        return None
    labels = {key: value for key, value in alert.labels.items() if key != "service_name"}
    read = adapter.query_alert_rule(alert.name, labels, alert.starts_at)
    identity = AlertRuleCapture(
        evidence_id=f"alert-rule:{alert.name}:FAILED",
        alert=alert.name,
        namespace=alert.namespace,
        service=alert.service,
        fired_at=alert.starts_at,
        failed=True,
    )
    if isinstance(read, ProviderReadFailure):
        return SymptomCapture(rule=identity)
    found, read_id = read
    expression = found.get("expression")
    rule = identity.model_copy(
        update={
            "evidence_id": f"provider-read:{read_id}",
            "failed": False,
            "expression": expression if isinstance(expression, str) else None,
            "for_seconds": _float(found.get("for_seconds")),
            "interval_seconds": _float(found.get("interval_seconds")),
            "scrape_interval_seconds": _float(found.get("scrape_interval_seconds")),
        }
    )
    form = recognize(rule.expression)
    if form is None or rule.for_seconds is None:
        return SymptomCapture(rule=rule)
    starts_at = alert.starts_at - timedelta(seconds=rule.for_seconds + form.range_seconds)
    entries = adapter.query_execution_records(
        alert.namespace, alert.service, starts_at, alert.starts_at
    )
    if isinstance(entries, ProviderReadFailure):
        failed = ExecutionRecordCapture(
            evidence_id=f"execution-record:{alert.service}:FAILED",
            namespace=alert.namespace,
            service=alert.service,
            starts_at=starts_at,
            ends_at=alert.starts_at,
            failed=True,
        )
        return SymptomCapture(rule=rule, execution=failed)
    records, records_id = entries
    return SymptomCapture(
        rule=rule,
        execution=execution_capture(
            f"provider-read:{records_id}",
            alert.namespace,
            alert.service,
            starts_at,
            alert.starts_at,
            records,
        ),
    )


def capture_symptoms(
    adapter: ProviderAdapter, alerts: Sequence[Alert]
) -> tuple[SymptomCapture, ...]:
    """Every firing alert's capture, once per alert occurrence; none where the providers serve neither read."""
    if not adapter.supports_symptom_capture():
        return ()
    seen = {(alert.name, alert.starts_at): alert for alert in alerts}
    return tuple(
        capture
        for _, alert in sorted(seen.items())
        if (capture := capture_symptom(adapter, alert)) is not None
    )


__all__ = ["capture_symptom", "capture_symptoms", "execution_capture"]
