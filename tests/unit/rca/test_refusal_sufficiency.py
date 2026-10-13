"""m21 §36: a dependency question answered by a symptom met by refusals at another peer."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import count

from rca_builders import at, microservice, ref

from packages.rca.evidence_coverage import TraceRead
from packages.rca.frontier import ANSWERED_STATES, DEPENDENCY_REFUSAL_SUFFICIENT_RULE
from packages.rca.model import (
    AlertRuleCapture,
    EntityRef,
    ExecutionOperation,
    ExecutionRecordCapture,
    ExecutionTerminal,
    FrontierAnswer,
    ObjectVersion,
    StructuralAlternative,
    SymptomCapture,
    TraceSpanObservation,
)
from packages.rca.refusal_sufficiency import ANSWER, dependency_refusal_sufficient, recognize

R = DEPENDENCY_REFUSAL_SUFFICIENT_RULE
FIRED = at(60)
ORDER = ref("shop/Deployment/order")
BROKER = ref("shop/Service/broker")
EXPRESSION = 'sum(rate(http_requests_total{service="order",status=~"5.."}[30s])) > 0.05'
CALLER_ENV = {"PAY_URL": "http://pay:8080", "BROKER_ADDR": "broker:9092"}


def topology(
    pay_env: dict[str, str] | None = None,
) -> tuple[
    dict[EntityRef, ObjectVersion], dict[EntityRef, set[EntityRef]], dict[EntityRef, EntityRef]
]:
    latest = {
        v.entity: v
        for v in (
            *microservice("order", 0, CALLER_ENV),
            *microservice("pay", 0, pay_env),
            *microservice("broker", 0),
        )
    }
    selects = {
        ref(f"shop/Service/{n}"): {ref(f"shop/Pod/{n}-5d8f7c9b4-abcde")}
        for n in ("order", "pay", "broker")
    }
    owned_by = {
        **{
            ref(f"shop/Pod/{n}-5d8f7c9b4-abcde"): ref(f"shop/ReplicaSet/{n}-5d8f7c9b4")
            for n in ("order", "pay", "broker")
        },
        **{
            ref(f"shop/ReplicaSet/{n}-5d8f7c9b4"): ref(f"shop/Deployment/{n}")
            for n in ("order", "pay", "broker")
        },
    }
    return latest, selects, owned_by


_ids = count()


def request(
    ended: datetime,
    *operations: tuple[str, str, str | None],
    status: int = 503,
) -> tuple[ExecutionTerminal, list[ExecutionOperation]]:
    """One request ending at ``ended``: its operations as (server address, outcome, error type), in order."""
    trace = f"t{next(_ids)}"
    ops = [
        ExecutionOperation(
            trace_id=trace,
            started_at=ended - timedelta(seconds=0.1) + timedelta(milliseconds=i),
            duration_seconds=0.001,
            kind="HTTP_CLIENT",
            outcome=outcome,
            server_address=address,
            error_type=error,
        )
        for i, (address, outcome, error) in enumerate(operations)
    ]
    return ExecutionTerminal(trace_id=trace, at=ended, status=status, path="/orders"), ops


DB_OK = ("db", "OK", None)
REFUSED = ("pay", "ERROR", "ConnectionRefusedError")


def refusals(
    *operations: tuple[str, str, str | None], every: float = 3.0
) -> list[tuple[ExecutionTerminal, list[ExecutionOperation]]]:
    """Requests refused at the peer every ``every`` seconds over the minute before the firing."""
    ops = operations or (DB_OK, REFUSED)
    return [request(FIRED - timedelta(seconds=every * k), *ops) for k in range(int(60 / every))]


TARGET = {"job": "order", "instance": "order.shop.svc:8000"}


def scrapes(offset: float = 2.0, count: int = 13) -> list[datetime]:
    """Scrape times every 5 s, the last ``offset`` seconds before the firing."""
    return [FIRED - timedelta(seconds=offset + 5 * k) for k in reversed(range(count))]


def counter(
    times: list[datetime] | None = None, status: str = "503", **labels: str
) -> dict[str, object]:
    return {
        "labels": {**TARGET, "service": "order", "route": "/orders", "status": status, **labels},
        "samples": [
            {"at": at, "value": float(i)}
            for i, at in enumerate(scrapes() if times is None else times)
        ],
    }


def durations(times: list[datetime] | None = None, seconds: float = 0.01) -> dict[str, object]:
    return {
        "labels": TARGET,
        "samples": [{"at": at, "value": seconds} for at in (scrapes() if times is None else times)],
    }


def capture(
    requests: list[tuple[ExecutionTerminal, list[ExecutionOperation]]] | None = None,
    **rule: object,
) -> SymptomCapture:
    requests = refusals() if requests is None else requests
    return SymptomCapture(
        rule=AlertRuleCapture.model_validate(
            {
                "evidence_id": "rule:OrderErrorRateHigh",
                "alert": "OrderErrorRateHigh",
                "fired_at": FIRED,
                "namespace": "shop",
                "service": "order",
                "expression": EXPRESSION,
                "for_seconds": 15.0,
                "interval_seconds": 5.0,
                "scrape_interval_seconds": 5.0,
                "counter_series": [counter()],
                "scrape_durations": [durations()],
                **rule,
            }
        ),
        execution=ExecutionRecordCapture(
            evidence_id="execution:order",
            namespace="shop",
            service="order",
            starts_at=FIRED - timedelta(seconds=45),
            ends_at=FIRED,
            terminals=tuple(t for t, _ in requests),
            operations=tuple(o for _, ops in requests for o in ops),
        ),
    )


def question(actor: EntityRef = BROKER, role: str = "dependency") -> StructuralAlternative:
    return StructuralAlternative(
        alternative_id=f"alternative:{actor.name}",
        actor=actor,
        role=role,
        affected_entities=(ORDER,),
        material_for_hypothesis_ids=("leader",),
    )


def answer(
    *captures: SymptomCapture,
    alternative: StructuralAlternative | None = None,
    pay_env: dict[str, str] | None = None,
    spans: list[TraceSpanObservation] | None = None,
    reads: list[TraceRead] | None = None,
) -> FrontierAnswer | None:
    latest, selects, owned_by = topology(pay_env)
    return dependency_refusal_sufficient(
        alternative or question(),
        captures,
        latest,
        selects,
        owned_by,
        spans or [],
        reads,
        FIRED,
    )


def state(found: FrontierAnswer | None) -> tuple[str, str | None, str] | None:
    return None if found is None else (found.state, found.blocked_reason, found.rule_id)


def test_refusals_at_a_peer_that_does_not_use_the_dependency_answer_the_question() -> None:
    found = answer(capture())
    assert state(found) == (ANSWER, None, R)
    assert ANSWER in ANSWERED_STATES
    assert found is not None
    assert found.evidence_ids == ("execution:order", "rule:OrderErrorRateHigh")
    assert found.affected_claims == ("leader",)
    assert found.remaining_uncertainty == ("AN_ADDITIONAL_CONTRIBUTION", "THE_PEER_S_OWN_CAUSE")


def test_a_run_without_the_captures_is_unchanged() -> None:
    # hard: every run stored before 2.15.0; the rule then leaves the earlier answer as it was
    assert answer() is None


def test_a_failed_read_is_not_captured() -> None:
    assert state(answer(capture(failed=True))) == ("OPEN", "NOT_CAPTURED", R)
    unread = capture().model_copy(update={"execution": None})
    assert state(answer(unread)) == ("OPEN", "NOT_CAPTURED", R)
    failed = capture()
    assert failed.execution is not None
    failed = failed.model_copy(
        update={"execution": failed.execution.model_copy(update={"failed": True})}
    )
    assert state(answer(failed)) == ("OPEN", "NOT_CAPTURED", R)


def test_only_the_counted_request_form_is_supported() -> None:
    for expression in (
        None,
        'sum(rate(http_requests_total{service="order",status=~"5.."}[30s])) / sum(rate(http_requests_total{service="order"}[30s])) > 0.05',
        'sum(rate(http_requests_total{service="order",status=~"[45].."}[30s])) > 0.05',
        'sum(rate(http_requests_total{service="order",status!~"2.."}[30s])) > 0.05',
        'sum(rate(http_requests_total{service="order",status=~"5..",path="/x"}[30s])) > 0.05',
        'sum(rate(http_requests_total{service=~"order",status=~"5.."}[30s])) > 0.05',
        'sum(rate(http_client_requests_total{service="order",status=~"5.."}[30s])) > 0.05',
        'sum(rate(http_requests_total{service="order",status=~"5.."}[30s])) < 0.05',
        'avg(rate(http_requests_total{service="order",status=~"5.."}[30s])) > 0.05',
    ):
        assert state(answer(capture(expression=expression))) == ("OPEN", "FORM_UNSUPPORTED", R), (
            expression
        )
    other = 'sum(rate(http_requests_total{service="pay",status=~"5.."}[30s])) > 0.05'
    assert state(answer(capture(expression=other))) == ("OPEN", "FORM_UNSUPPORTED", R)


def test_the_recognized_form() -> None:
    form = recognize(EXPRESSION)
    assert form is not None
    assert (form.service, form.range_seconds, form.threshold, form.inclusive) == (
        "order",
        30.0,
        0.05,
        False,
    )
    minutes = recognize(
        'sum(rate(http_requests_total{status=~"5[0-9][0-9]", service="order"}[1m])) >= 1'
    )
    assert minutes is not None
    assert (minutes.range_seconds, minutes.threshold, minutes.inclusive) == (60.0, 1.0, True)


def test_unknown_timing_keeps_the_question_open() -> None:
    for missing in ("for_seconds", "interval_seconds"):
        assert state(answer(capture(**{missing: None}))) == ("OPEN", "TIMING_UNKNOWN", R), missing


def test_a_request_that_reached_the_dependency_first_is_never_counted() -> None:
    # hard: an operation to D before the refusal; nothing else in the window
    reached = refusals(DB_OK, ("broker", "OK", None), REFUSED)
    found = answer(capture(reached))
    assert state(found) == ("OPEN", "NOT_SUFFICIENT", R)


def test_an_evaluation_below_the_threshold_leaves_the_question_open() -> None:
    # hard: refusals stop 24 s before the firing; the evaluation at the firing sees one request in its window
    late = [r for r in refusals() if r[0].at <= FIRED - timedelta(seconds=24)]
    found = answer(capture(late))
    assert state(found) == ("OPEN", "NOT_SUFFICIENT", R)
    assert found is not None and found.failed_evaluation_at == FIRED
    # two in 30 s meet a threshold of 0.05/s at every evaluation; one does not
    assert state(answer(capture(refusals(every=10.0)))) == (ANSWER, None, R)
    assert state(answer(capture(refusals(every=20.0)))) == ("OPEN", "NOT_SUFFICIENT", R)


def at_seconds(*seconds: float) -> list[tuple[ExecutionTerminal, list[ExecutionOperation]]]:
    return [request(FIRED - timedelta(seconds=s), DB_OK, REFUSED) for s in seconds]


# every evaluation but the firing's is met; the firing's sees one request (24 s before it)
BASE = (24.0, 27.0, 30.0, 33.0, 36.0, 39.0)


def test_only_requests_between_the_visible_samples_count() -> None:
    found = answer(capture(at_seconds(*BASE)))
    assert state(found) == ("OPEN", "NOT_SUFFICIENT", R)
    assert found is not None and found.failed_evaluation_at == FIRED
    # the last sample before the firing is 2 s before it: a request after it is in no increase yet
    assert state(answer(capture(at_seconds(*BASE, 0.2, 1.0, 1.6)))) == ("OPEN", "NOT_SUFFICIENT", R)
    # within MARGIN of that sample: not counted either
    assert state(answer(capture(at_seconds(*BASE, 2.1, 2.4)))) == ("OPEN", "NOT_SUFFICIENT", R)
    # just before it: counted, and the firing's evaluation is met
    assert state(answer(capture(at_seconds(*BASE, 2.6)))) == (ANSWER, None, R)


def test_the_pending_evaluation_is_met_by_the_requests_its_samples_saw() -> None:
    # dev25: the errors that made the alert pending came seconds before that evaluation; §36.3's fixed window
    # [t - R + S, t - S] left them out, the samples' own interval does not
    burst = [request(FIRED - timedelta(seconds=15 + s), DB_OK, REFUSED) for s in (2.6, 3.0, 3.5)]
    steady = [request(FIRED - timedelta(seconds=s), DB_OK, REFUSED) for s in range(3, 15)]
    found = answer(capture(burst + steady))
    assert state(found) == (ANSWER, None, R)


def test_a_sample_not_yet_stored_at_an_evaluation_is_not_visible() -> None:
    requests = at_seconds(*BASE, 2.6)
    assert state(answer(capture(requests))) == (ANSWER, None, R)
    # a scrape that took 2 s was stored after the evaluation: the last sample it saw is 7 s before the firing
    slow = durations(seconds=2.0)
    assert state(answer(capture(requests, scrape_durations=[slow]))) == (
        "OPEN",
        "NOT_SUFFICIENT",
        R,
    )
    # without a duration no sample is visible
    assert state(answer(capture(requests, scrape_durations=[]))) == ("OPEN", "NOT_SUFFICIENT", R)


def test_a_counter_read_through_another_target_or_exporter_is_unsupported() -> None:
    other = counter(job="otel-collector", instance="otel:8889")
    assert state(answer(capture(counter_series=[counter(), other]))) == (
        "OPEN",
        "FORM_UNSUPPORTED",
        R,
    )
    exported = counter(exported_job="order")
    assert state(answer(capture(counter_series=[exported]))) == ("OPEN", "FORM_UNSUPPORTED", R)


def test_a_capture_without_the_samples_is_not_captured() -> None:
    assert state(answer(capture(counter_series=None))) == ("OPEN", "NOT_CAPTURED", R)
    assert state(answer(capture(scrape_durations=None))) == ("OPEN", "NOT_CAPTURED", R)


def test_a_request_counts_only_in_its_own_series() -> None:
    assert state(answer(capture(counter_series=[counter(status="500")]))) == (
        "OPEN",
        "NOT_SUFFICIENT",
        R,
    )
    other_route = counter()
    other_route["labels"] = {**other_route["labels"], "route": "/health"}  # type: ignore[dict-item]
    assert state(answer(capture(counter_series=[other_route]))) == ("OPEN", "NOT_SUFFICIENT", R)


def test_a_counter_reset_leaves_its_series_out() -> None:
    reset = counter()
    reset["samples"] = [{"at": at, "value": float(i % 4)} for i, at in enumerate(scrapes())]
    assert state(answer(capture(counter_series=[reset]))) == ("OPEN", "NOT_SUFFICIENT", R)


def test_only_a_refusal_at_a_declared_peer_counts() -> None:
    timeouts = refusals(DB_OK, ("pay", "ERROR", "TimeoutError"))
    assert state(answer(capture(timeouts))) == ("OPEN", "NOT_SUFFICIENT", R)
    undeclared = refusals(DB_OK, ("pay-2", "ERROR", "ConnectionRefusedError"))
    assert state(answer(capture(undeclared))) == ("OPEN", "NOT_SUFFICIENT", R)
    at_d = refusals(DB_OK, ("broker", "ERROR", "ConnectionRefusedError"))
    assert state(answer(capture(at_d))) == ("OPEN", "NOT_SUFFICIENT", R)
    ok = refusals(DB_OK, REFUSED)
    ok = [(t.model_copy(update={"status": 200}), ops) for t, ops in ok]
    assert state(answer(capture(ok))) == ("OPEN", "NOT_SUFFICIENT", R)


def test_the_terminal_operation_is_the_last_failed_one() -> None:
    # a refusal recovered by a later failure elsewhere is not the request's terminal operation
    later = refusals(REFUSED, ("db", "ERROR", "OperationalError"))
    assert state(answer(capture(later))) == ("OPEN", "NOT_SUFFICIENT", R)
    retried = refusals(REFUSED, ("broker", "OK", None), REFUSED)
    assert state(answer(capture(retried))) == ("OPEN", "NOT_SUFFICIENT", R)


def _pay_spans(called: bool) -> list[TraceSpanObservation]:
    spans = []
    for n, minutes in enumerate((40.0, 50.0, 58.0)):
        start = at(minutes)
        spans.append(
            TraceSpanObservation(
                trace_id=f"p{n}",
                span_id=f"ps{n}",
                service="pay",
                span_name="POST /pay",
                span_kind="SERVER",
                start_at=start,
                end_at=start + timedelta(seconds=0.01),
                evidence_id=f"span:p{n}",
            )
        )
        spans.append(
            TraceSpanObservation(
                trace_id=f"p{n}",
                span_id=f"pc{n}",
                service="pay",
                span_name="publish" if called else "query",
                span_kind="PRODUCER" if called else "CLIENT",
                start_at=start,
                end_at=start + timedelta(seconds=0.01),
                semantic_attributes={"server.address": "broker" if called else "ledger"},
                evidence_id=f"span:pc{n}",
            )
        )
    return spans


PAY_READS = [
    TraceRead(
        namespace="shop", service="pay", starts_at=at(0), ends_at=at(70), completeness="BEST_EFFORT"
    )
]


def test_a_peer_that_uses_the_dependency_answers_nothing() -> None:
    uses = {"BROKER_ADDR": "broker:9092", "LEDGER_HOST": "ledger"}
    assert state(answer(capture(), pay_env=uses, spans=_pay_spans(True), reads=PAY_READS)) == (
        "OPEN",
        "PEER_USES_IT",
        R,
    )
    # untraced: C cannot show the declaration unused
    assert state(answer(capture(), pay_env=uses)) == ("OPEN", "PEER_USES_IT", R)


def test_a_peer_that_declares_the_dependency_but_never_used_it_meets_condition_five() -> None:
    uses = {"BROKER_ADDR": "broker:9092", "LEDGER_HOST": "ledger"}
    found = answer(capture(), pay_env=uses, spans=_pay_spans(False), reads=PAY_READS)
    assert state(found) == (ANSWER, None, R)


def test_the_rule_applies_only_to_a_dependency_of_the_alerts_own_service() -> None:
    assert answer(capture(), alternative=question(role="configuration")) is None
    elsewhere = capture(service="pay")
    assert answer(elsewhere) is None


def test_one_sufficient_capture_answers_the_question() -> None:
    found = answer(capture(expression=None), capture())
    assert state(found) == (ANSWER, None, R)
