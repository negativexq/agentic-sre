"""m21 §31: a called dependency the symptom's anomalous requests never reached."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import count

from rca_builders import at, microservice, ref

from packages.rca.evidence_coverage import TraceRead
from packages.rca.frontier import (
    DEPENDENCY_DECLARED_UNUSED_RULE,
    DEPENDENCY_NOT_REACHED_RULE,
    dependency_not_reached_by_anomaly,
)
from packages.rca.model import (
    EntityRef,
    FrontierAnswer,
    ObjectVersion,
    StructuralAlternative,
    TraceSpanObservation,
    TraceSpanStatus,
)

ONSET = at(60)
CUTOFF = at(62)
ORDER = ref("shop/Deployment/order")
WORKER = ref("shop/Deployment/worker")
BROKER = ref("shop/Service/broker")
PAY = ref("shop/Service/pay")
LATEST: dict[EntityRef, ObjectVersion] = {
    v.entity: v
    for v in (
        *microservice("order", 0, {"BROKER_ADDR": "broker:9092", "PAY_URL": "http://pay:8080"}),
        *microservice("worker", 0, {"BROKER_ADDR": "broker:9092"}),
        *microservice("broker", 0),
        *microservice("pay", 0),
    )
}
READS = [
    TraceRead(
        namespace="shop", service=name, starts_at=at(0), ends_at=at(70), completeness="TRUNCATED"
    )
    for name in ("order", "worker")
]
_ids = count()
OK, ERROR = TraceSpanStatus.OK, TraceSpanStatus.ERROR


def _span(
    trace: str,
    parent: str | None,
    start: datetime,
    seconds: float,
    *,
    kind: str,
    address: str | None = None,
    status: TraceSpanStatus = OK,
    service: str = "order",
) -> TraceSpanObservation:
    n = next(_ids)
    return TraceSpanObservation(
        trace_id=trace,
        span_id=f"s{n}",
        parent_span_id=parent,
        service=service,
        span_name="span",
        span_kind=kind,
        start_at=start,
        end_at=start + timedelta(seconds=seconds),
        status=status,
        semantic_attributes={"server.address": address} if address else {},
        evidence_id=f"span:{n}",
    )


def request(
    minutes: float,
    *,
    status: TraceSpanStatus = OK,
    seconds: float = 0.1,
    pay: tuple[TraceSpanStatus, float] | None = (OK, 0.05),
    broker: bool = True,
    extra: tuple[tuple[str, str | None, TraceSpanStatus, float], ...] = (),
) -> list[TraceSpanObservation]:
    """One request of ``order``: its SERVER span, a payment call and, unless it failed first, a publish."""
    trace = f"t{next(_ids)}"
    start = at(minutes)
    server = _span(trace, None, start, seconds, kind="SERVER", status=status)
    spans = [server]
    if pay is not None:
        spans.append(
            _span(trace, server.span_id, start, pay[1], kind="CLIENT", address="pay", status=pay[0])
        )
    if broker:
        spans.append(_span(trace, server.span_id, start, 0.01, kind="PRODUCER", address="broker"))
    for kind, address, call_status, took in extra:
        spans.append(
            _span(
                trace, server.span_id, start, took, kind=kind, address=address, status=call_status
            )
        )
    return spans


def baseline(n: int = 3) -> list[TraceSpanObservation]:
    return [span for m in (50, 52, 54)[:n] for span in request(m)]


def failing(n: int = 3) -> list[TraceSpanObservation]:
    """Requests that failed at the payment call and never published."""
    return [
        span
        for i in range(n)
        for span in request(60.1 + 0.2 * i, status=ERROR, pay=(ERROR, 0.05), broker=False)
    ]


def question(actor: EntityRef = BROKER, *callers: EntityRef) -> StructuralAlternative:
    return StructuralAlternative(
        alternative_id=f"alternative:{actor.name}",
        actor=actor,
        role="dependency",
        affected_entities=callers or (ORDER,),
        material_for_hypothesis_ids=("leader",),
    )


def answer(
    spans: list[TraceSpanObservation],
    *,
    alternative: StructuralAlternative | None = None,
    reads: list[TraceRead] | None = READS,
    cutoff: datetime | None = CUTOFF,
) -> FrontierAnswer | None:
    return dependency_not_reached_by_anomaly(
        alternative or question(), LATEST, spans, reads, ONSET, cutoff
    )


def state(found: FrontierAnswer | None) -> tuple[str, str | None] | None:
    return (found.state, found.blocked_reason) if found is not None else None


def test_anomalous_requests_that_failed_elsewhere_and_never_reached_it_answer_the_question() -> (
    None
):
    spans = baseline() + failing()
    found = answer(spans)
    assert state(found) == ("ANSWERED_NOT_REACHED", None)
    assert found is not None and found.rule_id == DEPENDENCY_NOT_REACHED_RULE
    assert found.affected_claims == ("leader",)
    assert found.remaining_uncertainty == (
        "AN_EFFECT_OUTSIDE_THE_REQUESTS",
        "ANOMALIES_OUTSIDE_THE_WINDOW",
        "THE_SAMPLE_OF_A_CUT_READ",
    )
    # the evidence is each anomalous request and the anomalous call that locates it
    flagged = [s for s in failing_spans(spans) if s.span_kind in ("SERVER", "CLIENT")]
    assert set(found.evidence_ids) == {s.evidence_id for s in flagged}


def failing_spans(spans: list[TraceSpanObservation]) -> list[TraceSpanObservation]:
    return [s for s in spans if s.start_at >= ONSET]


def test_hard_an_anomalous_request_that_reached_the_dependency_is_never_answered() -> None:
    reached = request(60.8, status=ERROR, pay=(ERROR, 0.05), broker=True)
    assert state(answer(baseline() + failing() + reached)) == ("OPEN", "REACHED")
    # a call below the request through the caller's own span reaches it too
    trace = "deep"
    server = _span(trace, None, at(60.9), 0.1, kind="SERVER", status=ERROR)
    pay = _span(trace, server.span_id, at(60.9), 0.05, kind="CLIENT", address="pay", status=ERROR)
    publish = _span(trace, pay.span_id, at(60.9), 0.01, kind="PRODUCER", address="broker")
    assert state(answer(baseline() + failing() + [server, pay, publish])) == ("OPEN", "REACHED")
    # a healthy request's call does not count; only anomalous requests are read
    assert state(answer(baseline() + failing() + request(60.5))) == ("ANSWERED_NOT_REACHED", None)


def test_a_slow_request_located_at_a_slow_call_is_anomalous() -> None:
    slow = [
        span
        for i in range(3)
        for span in request(60.1 + 0.2 * i, seconds=1.0, pay=(OK, 0.9), broker=False)
    ]
    assert state(answer(baseline() + slow)) == ("ANSWERED_NOT_REACHED", None)
    # not slower than 3 x the baseline median: not anomalous
    mild = [s for i in range(3) for s in request(60.1 + 0.2 * i, seconds=0.25, broker=False)]
    assert state(answer(baseline() + mild)) == ("OPEN", "TOO_FEW_ANOMALOUS_REQUESTS")


def test_the_window_must_be_complete() -> None:
    assert state(answer(baseline() + failing(), cutoff=at(60.9))) == ("OPEN", "WINDOW_NOT_COMPLETE")


def test_the_caller_needs_three_baseline_requests() -> None:
    assert state(answer(baseline(2) + failing())) == ("OPEN", "NO_BASELINE")
    # a caller with no SERVER span, as a consumer, has no baseline
    consumer = [
        _span("w", None, at(m), 0.01, kind="CONSUMER", service="worker") for m in (50, 52, 60.5)
    ]
    found = answer(baseline() + failing() + consumer, alternative=question(BROKER, ORDER, WORKER))
    assert state(found) == ("OPEN", "NO_BASELINE")


def test_three_anomalous_requests_in_the_window() -> None:
    assert state(answer(baseline() + failing(2))) == ("OPEN", "TOO_FEW_ANOMALOUS_REQUESTS")
    # anomalies after the window are not read
    late = [s for s in request(61.5, status=ERROR, pay=(ERROR, 0.05), broker=False)]
    assert state(answer(baseline() + failing(2) + late)) == ("OPEN", "TOO_FEW_ANOMALOUS_REQUESTS")


def test_each_anomalous_request_must_have_an_anomalous_call_to_another_peer() -> None:
    unlocated = request(60.8, status=ERROR, pay=(OK, 0.05), broker=False)
    assert state(answer(baseline() + failing() + unlocated)) == ("OPEN", "ANOMALY_NOT_LOCATED")
    # a peer without baseline calls is anomalous only by ERROR
    new_peer = (("CLIENT", "pay", OK, 0.05),)
    slow_new = request(
        60.8, status=ERROR, pay=None, broker=False, extra=(("CLIENT", "db", OK, 5.0), *new_peer)
    )
    assert state(answer(baseline() + failing() + slow_new)) == ("OPEN", "ANOMALY_NOT_LOCATED")
    failed_new = request(
        60.8, status=ERROR, pay=None, broker=False, extra=(("CLIENT", "db", ERROR, 0.01),)
    )
    assert state(answer(baseline() + failing() + failed_new)) == ("ANSWERED_NOT_REACHED", None)


def test_the_callers_calls_must_be_traced() -> None:
    untraced = request(60.5, extra=(("CLIENT", None, OK, 0.01),))
    assert state(answer(baseline() + failing() + untraced)) == ("OPEN", "CALLS_NOT_TRACED")


def test_a_failed_read_of_the_caller_keeps_it_open_and_a_cut_one_does_not() -> None:
    failed = [r.model_copy(update={"completeness": "FAILED"}) for r in READS]
    assert state(answer(baseline() + failing(), reads=failed)) == ("OPEN", "TRACE_READ_FAILED")


def test_every_caller_must_meet_the_conditions() -> None:
    worker = [
        _span(f"w{m}", None, at(m), 0.1, kind="SERVER", service="worker") for m in (50, 52, 54)
    ]
    found = answer(baseline() + failing() + worker, alternative=question(BROKER, ORDER, WORKER))
    assert state(found) == ("OPEN", "TOO_FEW_ANOMALOUS_REQUESTS")


def test_the_rule_needs_a_dependency_question_a_declaration_reads_and_a_cutoff() -> None:
    spans = baseline() + failing()
    assert answer(spans, reads=None) is None
    assert answer(spans, cutoff=None) is None
    assert answer(spans, alternative=question(ref("shop/Service/elsewhere"))) is None
    other = question().model_copy(update={"role": "configuration_source"})
    assert answer(spans, alternative=other) is None
    assert dependency_not_reached_by_anomaly(question(), LATEST, spans, READS, None, CUTOFF) is None


def test_duplicate_observations_of_a_span_are_read_once() -> None:
    spans = baseline() + failing(2)
    assert state(answer(spans + spans)) == ("OPEN", "TOO_FEW_ANOMALOUS_REQUESTS")


def test_in_the_resolution_it_follows_c_only_for_a_called_dependency() -> None:
    from test_dependency_frontier import FAULT, POD, claim

    from packages.rca.resolution import resolve_hypotheses

    claims = [claim("leader", FAULT, POD, PAY, ORDER), claim("aside", POD, ORDER)]
    spans = baseline() + failing()

    def resolved(cutoff: datetime) -> tuple[str, str | None, str]:
        trace = resolve_hypotheses(
            claims,
            structural_alternatives=[question()],
            trace_spans=spans,
            latest=LATEST,
            trace_reads=READS,
            causal_onset=ONSET,
            observation_cutoff=cutoff,
        )
        (found,) = [a for a in trace.frontier_answers if a.alternative_id == "alternative:broker"]
        return found.state, found.blocked_reason, found.rule_id

    assert resolved(CUTOFF) == ("ANSWERED_NOT_REACHED", None, DEPENDENCY_NOT_REACHED_RULE)
    # an open §31 answer names its condition in place of C's CALLED
    assert resolved(at(60.5)) == ("OPEN", "WINDOW_NOT_COMPLETE", DEPENDENCY_NOT_REACHED_RULE)
    # a dependency never called up to the onset stays C's
    unused = resolve_hypotheses(
        claims,
        structural_alternatives=[question()],
        trace_spans=[s for s in spans if s.span_kind != "PRODUCER"],
        latest=LATEST,
        trace_reads=READS,
        causal_onset=ONSET,
        observation_cutoff=CUTOFF,
    )
    (found,) = unused.frontier_answers
    assert found.rule_id == DEPENDENCY_DECLARED_UNUSED_RULE
