"""Read-only reconciliation of the caller's request ledger against the workload's own records.

The testbed's traffic driver records every request it sends, with a trace id it chose
(``.local/testbed/ledgers/<database>.jsonl``). Each request is looked up in Tempo by that trace id, in
order-service's ExecutionRecord (start, operation and terminal records in Loki), then in the workload's
``orders`` and ``payments`` tables and in the worker's processed set in Redis. The ledger is the only
channel independent of the workload process (design §11, decision 5); per-pod lifecycle balance of the
ExecutionRecord is reported beside it.
Nothing here feeds the engine; it measures whether telemetry accounts for every request the caller
made (symptom explanation design, checkpoint 1). The workload's Postgres and Redis are reached through
port-forwards the operator opens.

Usage: ``python -m scripts.request_ledger_reconcile LEDGER.jsonl OUT.jsonl
[--tempo URL] [--loki URL] [--workload-db DSN] [--redis URL]``.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import bindparam, create_engine, text

EVENT_NAMESPACE = UUID("d9e0822c-d6dc-4e7d-aea3-59f30df796fb")  # workload/order_service/app.py
PROCESSED_PREFIX = "agentic-sre:processed:"  # workload/common/adapters.py RedisStateStore


@dataclass(frozen=True, slots=True)
class TraceFacts:
    """What one trace says about one request."""

    server: bool = False  # order-service's server span is in it
    order_id: str | None = None
    payment_called: bool = False
    payment_error: str | None = None
    published: bool = False
    publish_error: str | None = None


@dataclass(frozen=True, slots=True)
class Operation:
    started_ns: int
    kind: str
    outcome: str
    error_type: str | None
    server_address: str | None


@dataclass(slots=True)
class ExecutionFacts:
    """One request's ExecutionRecord as order-service wrote it."""

    start: bool = False
    terminal_status: int | None = None
    pod_uid: str | None = None
    operations: list[Operation] = field(default_factory=list)

    @property
    def terminal_operation(self) -> Operation | None:
        """The last failed operation of a request that ended in a 5xx; None when none failed."""
        if self.terminal_status is None or self.terminal_status < 500:
            return None
        failed = [o for o in self.operations if o.outcome == "ERROR"]
        return max(failed, key=lambda o: o.started_ns) if failed else None


def execution_facts(entries: Iterable[Mapping[str, str]]) -> dict[str, ExecutionFacts]:
    """Each trace's ExecutionRecord, from Loki entries (stream labels plus structured metadata)."""
    facts: dict[str, ExecutionFacts] = defaultdict(ExecutionFacts)
    for entry in entries:
        trace_id = entry.get("trace_id")
        record = entry.get("execution_record")
        if not trace_id or not record:
            continue
        fact = facts[trace_id]
        fact.pod_uid = fact.pod_uid or entry.get("k8s_pod_uid") or None
        if record == "start":
            fact.start = True
        elif record == "terminal" and entry.get("http_response_status_code"):
            fact.terminal_status = int(entry["http_response_status_code"])
        elif record == "operation":
            fact.operations.append(
                Operation(
                    started_ns=int(entry.get("execution_operation_started_ns") or 0),
                    kind=entry.get("execution_operation_kind", "OTHER"),
                    outcome=entry.get("execution_operation_outcome", "OK"),
                    error_type=entry.get("error_type") or None,
                    server_address=entry.get("server_address") or None,
                )
            )
    return dict(facts)


def lifecycle(facts: Mapping[str, ExecutionFacts]) -> dict[str, dict[str, int]]:
    """Per pod: requests started, ended, and started without a terminal (a lifecycle gap, never a success)."""
    pods: dict[str, Counter[str]] = defaultdict(Counter)
    for fact in facts.values():
        pod = fact.pod_uid or "UNKNOWN_POD"
        pods[pod]["starts"] += fact.start
        pods[pod]["terminals"] += fact.terminal_status is not None
        pods[pod]["open"] += fact.start and fact.terminal_status is None
        pods[pod]["terminal_without_start"] += not fact.start and fact.terminal_status is not None
    return {pod: dict(counts) for pod, counts in pods.items()}


@dataclass(frozen=True, slots=True)
class Reconciled:
    trace_id: str
    caller_ok: bool
    caller_error: str | None
    trace_found: bool
    start_record: bool
    terminal_status: int | None
    terminal_operation: str | None
    order_id: str | None
    order_status: str | None
    payment_row: bool | None
    consumed: bool | None
    place: str


def _value(attribute: Mapping[str, Any]) -> Any:
    value = attribute.get("value") or {}
    return next(iter(value.values()), None) if isinstance(value, dict) else None


def _spans(document: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for batch in document.get("batches") or document.get("resourceSpans") or []:
        service = next(
            (
                _value(a)
                for a in (batch.get("resource") or {}).get("attributes", [])
                if a.get("key") == "service.name"
            ),
            "",
        )
        for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
            for span in scope.get("spans", []):
                yield str(service), span


def trace_facts(document: Mapping[str, Any]) -> TraceFacts:
    facts: dict[str, Any] = {}
    for service, span in _spans(document):
        attributes = {a.get("key"): _value(a) for a in span.get("attributes", [])}
        kind = str(span.get("kind", ""))
        error = attributes.get("error.type")
        if service == "order-service" and kind in {"SPAN_KIND_SERVER", "2"}:
            facts["server"] = True
            if attributes.get("app.order.id"):
                facts["order_id"] = str(attributes["app.order.id"])
        if service == "order-service" and span.get("name") == "POST payment-service /payments":
            facts["payment_called"] = True
            facts["payment_error"] = error or facts.get("payment_error")
        if service == "order-service" and attributes.get("messaging.system") == "kafka":
            facts["published"] = True
            facts["publish_error"] = error or facts.get("publish_error")
    return TraceFacts(**facts)


def place(
    caller_ok: bool,
    facts: TraceFacts | None,
    order_status: str | None,
    consumed: bool | None,
) -> str:
    """Where a request ended, from the records alone; ``UNPLACED`` when they do not say."""
    if facts is None and order_status is None:
        return "UNPLACED_NO_TRACE" if not caller_ok else "OK_NO_TRACE"
    if caller_ok:
        if consumed is False:
            return "OK_NOT_CONSUMED"
        return "OK"
    if order_status is None:
        if facts is not None and facts.server:
            return "FAILED_BEFORE_ORDER_ROW"
        return "UNPLACED"
    if order_status == "PENDING":
        return "FAILED_AT_PAYMENT_HOP"
    return "FAILED_AFTER_ORDER_UPDATE"


def _tempo(url: str, trace_id: str) -> Mapping[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{url}/api/traces/{trace_id}", timeout=10) as response:  # noqa: S310
            document = json.loads(response.read())
            return document if isinstance(document, dict) else None
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _loki(url: str, start: datetime, end: datetime) -> list[dict[str, str]]:
    """Every order-service ExecutionRecord entry in [start, end], paged forward in time."""
    seen: dict[tuple[int, str], dict[str, str]] = {}
    cursor = int(start.timestamp() * 1e9)
    stop = int(end.timestamp() * 1e9)
    query = '{service_name="order-service"} |= "execution."'
    while True:
        params = urllib.parse.urlencode(
            {"query": query, "start": cursor, "end": stop, "limit": 5000, "direction": "forward"}
        )
        with urllib.request.urlopen(
            f"{url}/loki/api/v1/query_range?{params}", timeout=60
        ) as response:  # noqa: S310
            result = json.loads(response.read())["data"]["result"]
        page = [(int(v[0]), dict(s["stream"]), v[1]) for s in result for v in s["values"]]
        for ts, labels, line in page:
            seen[(ts, json.dumps(labels, sort_keys=True) + line)] = labels
        if len(page) < 5000:
            return list(seen.values())
        last = max(ts for ts, _, _ in page)
        if last == cursor:
            raise RuntimeError("more than one Loki page of entries share one nanosecond")
        cursor = last  # the last nanosecond is read again; duplicates are dropped by key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger")
    parser.add_argument("out")
    parser.add_argument("--tempo", default="http://127.0.0.1:13200")
    parser.add_argument("--loki", default="http://127.0.0.1:13100")
    parser.add_argument("--workload-db")
    parser.add_argument("--redis")
    args = parser.parse_args()

    rows = [json.loads(line) for line in open(args.ledger) if line.strip()]
    rows = [r for r in rows if r.get("phase", "run") == "run"]
    first = min(datetime.fromisoformat(r["started"]) for r in rows)
    last = max(datetime.fromisoformat(r["finished"]) for r in rows)
    executions = execution_facts(_loki(args.loki, first, last))
    facts: dict[str, TraceFacts | None] = {}
    for row in rows:
        document = _tempo(args.tempo, row["trace_id"])
        facts[row["trace_id"]] = trace_facts(document) if document else None
    order_ids = {
        row["trace_id"]: row.get("order_id")
        or (facts[row["trace_id"]].order_id if facts[row["trace_id"]] else None)  # type: ignore[union-attr]
        for row in rows
    }
    known = sorted({o for o in order_ids.values() if o})

    statuses: dict[str, str] = {}
    payments: set[str] = set()
    if args.workload_db and known:
        engine = create_engine(args.workload_db, execution_options={"postgresql_readonly": True})
        with engine.connect() as connection:
            ids = [UUID(o) for o in known]
            for order_id, status in connection.execute(
                text("select order_id, status from orders where order_id in :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": ids},
            ):
                statuses[str(order_id)] = str(status).upper()
            payments = {
                str(r[0])
                for r in connection.execute(
                    text("select order_id from payments where order_id in :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": ids},
                )
            }
        engine.dispose()
    consumed: dict[str, bool] = {}
    if args.redis and known:
        import redis

        client = redis.Redis.from_url(args.redis)
        for order_id in known:
            event_id = uuid5(EVENT_NAMESPACE, order_id)
            consumed[order_id] = bool(client.exists(f"{PROCESSED_PREFIX}{event_id}"))

    counts: Counter[str] = Counter()
    with open(args.out, "w") as output:
        for row in rows:
            trace_id = row["trace_id"]
            order_id = order_ids[trace_id]
            status = statuses.get(order_id) if order_id else None
            execution = executions.get(trace_id)
            terminal = execution.terminal_operation if execution else None
            result = Reconciled(
                trace_id=trace_id,
                caller_ok=bool(row["ok"]),
                caller_error=row.get("error"),
                trace_found=facts[trace_id] is not None,
                start_record=bool(execution and execution.start),
                terminal_status=execution.terminal_status if execution else None,
                terminal_operation=(
                    f"{terminal.kind}:{terminal.server_address}:{terminal.error_type}"
                    if terminal
                    else None
                ),
                order_id=order_id,
                order_status=status,
                payment_row=(order_id in payments) if order_id and args.workload_db else None,
                consumed=consumed.get(order_id) if order_id else None,
                place=place(
                    bool(row["ok"]),
                    facts[trace_id],
                    status,
                    consumed.get(order_id) if order_id else None,
                ),
            )
            output.write(json.dumps(asdict(result)) + "\n")
            counts["requests"] += 1
            counts["trace_found"] += result.trace_found
            counts["start_record"] += result.start_record
            counts["terminal_record"] += result.terminal_status is not None
            if result.terminal_operation:
                counts[f"terminal_operation {result.terminal_operation}"] += 1
            counts["caller_failed"] += not result.caller_ok
            counts[result.place] += 1
    print(dict(counts))
    # the population the ledger does not see (other callers, probes) is part of the lifecycle too
    print("lifecycle per pod (all order-service requests in the window):", lifecycle(executions))


if __name__ == "__main__":
    main()
