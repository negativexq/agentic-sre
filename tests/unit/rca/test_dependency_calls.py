"""m21 §29 B and C: a dependency observed healthy, or declared and never used by its traced caller (§29.6)."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import count

from rca_builders import at, microservice, ref

from packages.rca.evidence_coverage import TraceRead
from packages.rca.frontier import (
    DEPENDENCY_DECLARED_UNUSED_RULE,
    DEPENDENCY_OBSERVED_HEALTHY_RULE,
    dependency_calls,
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
ORDER = ref("shop/Deployment/order")
BROKER = ref("shop/Service/broker")
CACHE = ref("shop/Service/cache")
DB = ref("shop/Service/db")
LATEST: dict[EntityRef, ObjectVersion] = {
    v.entity: v
    for v in (
        *microservice(
            "order",
            0,
            {"BROKER_ADDR": "broker:9092", "CACHE_URL": "redis://cache:6379/0", "DB_HOST": "db"},
        ),
        *microservice("broker", 0),
        *microservice("cache", 0),
        *microservice("db", 0),
    )
}
_ids = count()


def span(
    minutes: float,
    *,
    kind: str = "PRODUCER",
    address: str | None = "broker",
    seconds: float | None = 0.01,
    status: TraceSpanStatus = TraceSpanStatus.UNSET,
    service: str = "order",
    name: str = "publish",
) -> TraceSpanObservation:
    start: datetime = at(minutes)
    n = next(_ids)
    return TraceSpanObservation(
        trace_id=f"t{n}",
        span_id=f"s{n}",
        service=service,
        span_name=name,
        span_kind=kind,
        start_at=start,
        end_at=start + timedelta(seconds=seconds) if seconds is not None else None,
        status=status,
        semantic_attributes={"server.address": address} if address else {},
        evidence_id=f"span:{n}",
    )


def traffic(**overrides: object) -> list[TraceSpanObservation]:
    """Server spans of the caller, earlier broker calls, three recent ones and a database call."""
    recent = {"seconds": overrides.pop("recent_seconds", 0.012)}
    recent_status = overrides.pop("recent_status", TraceSpanStatus.UNSET)
    return [
        *(span(m, kind="SERVER", address=None) for m in (40, 50, 58)),
        *(span(m) for m in (40, 45, 50)),
        *(span(m, status=recent_status, **recent) for m in (56, 57, 59)),  # type: ignore[arg-type]
        span(58, kind="CLIENT", address="db"),
    ]


READS = [
    TraceRead(
        namespace="shop",
        service="order",
        starts_at=at(0),
        ends_at=at(70),
        completeness="BEST_EFFORT",
    )
]


def question(actor: EntityRef, caller: EntityRef = ORDER) -> StructuralAlternative:
    return StructuralAlternative(
        alternative_id=f"alternative:{actor.name}",
        actor=actor,
        role="dependency",
        affected_entities=(caller,),
        material_for_hypothesis_ids=("leader",),
    )


def answer(
    actor: EntityRef = BROKER,
    spans: list[TraceSpanObservation] | None = None,
    *,
    reads: list[TraceRead] | None = READS,
    alerted: tuple[EntityRef, ...] = (),
) -> FrontierAnswer | None:
    return dependency_calls(
        question(actor), LATEST, traffic() if spans is None else spans, reads, alerted, ONSET
    )


def state(found: FrontierAnswer | None) -> tuple[str, str | None, str] | None:
    return None if found is None else (found.state, found.blocked_reason, found.rule_id)


B, C = DEPENDENCY_OBSERVED_HEALTHY_RULE, DEPENDENCY_DECLARED_UNUSED_RULE


def test_healthy_calls_just_before_the_onset_answer_the_question() -> None:
    found = answer()
    assert state(found) == ("ANSWERED_OBSERVED_HEALTHY", None, B)
    assert found is not None and len(found.evidence_ids) == 3
    assert found.affected_claims == ("leader",)


def test_each_condition_of_b_keeps_the_question_open() -> None:
    assert state(answer(spans=traffic()[:-2])) == ("OPEN", "TOO_FEW_CALLS", B)
    failed = traffic(recent_status=TraceSpanStatus.ERROR)
    assert state(answer(spans=failed)) == ("OPEN", "FAILED_CALLS", B)
    assert state(answer(spans=traffic(recent_seconds=0.05))) == ("OPEN", "SLOWER_CALLS", B)
    assert state(answer(spans=traffic(recent_seconds=None))) == ("OPEN", "SLOWER_CALLS", B)
    no_earlier = [s for s in traffic() if not (s.span_kind == "PRODUCER" and s.start_at < at(55))]
    assert state(answer(spans=no_earlier)) == ("OPEN", "NO_EARLIER_CALLS", B)
    failed_read = [*READS, READS[0].model_copy(update={"completeness": "FAILED"})]
    assert state(answer(reads=failed_read)) == ("OPEN", "TRACE_READ_FAILED", B)
    assert state(answer(alerted=(BROKER,))) == ("OPEN", "DEPENDENCY_ALERTED", B)


def test_twice_as_slow_is_still_healthy() -> None:
    assert state(answer(spans=traffic(recent_seconds=0.02)))[0] == "ANSWERED_OBSERVED_HEALTHY"  # type: ignore[index]


def test_only_server_address_attributes_a_call() -> None:
    # spans that would name the broker by their name, kind or service carry no declared host: not calls to it
    unnamed = [
        s.model_copy(update={"semantic_attributes": {}}) if s.span_kind == "PRODUCER" else s
        for s in traffic()
    ]
    assert state(answer(spans=unnamed)) == ("OPEN", "CALLS_NOT_TRACED", C)
    other_host = [
        s.model_copy(update={"semantic_attributes": {"server.address": "broker-2"}})
        if s.span_kind == "PRODUCER"
        else s
        for s in traffic()
    ]
    assert state(answer(spans=other_host)) == ("ANSWERED_DECLARED_UNUSED", None, C)
    callee_side = [
        *traffic()[:3],
        *(span(m, service="broker", kind="SERVER", address=None) for m in (56, 57, 59)),
    ]
    assert state(answer(spans=[*callee_side, span(58, kind="CLIENT", address="db")]))[2] == C  # type: ignore[index]


def test_a_declaration_the_traced_caller_never_used_answers_the_question() -> None:
    found = answer(CACHE)
    assert state(found) == ("ANSWERED_DECLARED_UNUSED", None, C)
    assert found is not None and found.evidence_ids


def test_each_condition_of_c_keeps_the_question_open() -> None:
    no_server = [s for s in traffic() if s.span_kind != "SERVER"]
    assert state(answer(CACHE, no_server)) == ("OPEN", "CALLER_NOT_TRACED", C)
    unattributed = [*traffic(), span(58, kind="INTERNAL", address=None)]
    assert state(answer(CACHE, unattributed)) == ("OPEN", "CALLS_NOT_TRACED", C)
    no_outbound = [s for s in traffic() if s.span_kind == "SERVER"]
    assert state(answer(CACHE, no_outbound)) == ("OPEN", "CALLS_NOT_TRACED", C)
    failed_read = [READS[0].model_copy(update={"completeness": "FAILED"})]
    assert state(answer(CACHE, reads=failed_read)) == ("OPEN", "TRACE_READ_FAILED", C)
    called = [*traffic(), span(30, kind="CLIENT", address="cache")]
    assert state(answer(CACHE, called)) == ("OPEN", "TOO_FEW_CALLS", B)


def test_a_call_after_the_onset_is_outside_the_window() -> None:
    after = [*traffic(), span(61, kind="CLIENT", address="cache")]
    assert state(answer(CACHE, after)) == ("ANSWERED_DECLARED_UNUSED", None, C)


def test_the_rules_need_a_dependency_question_a_declaration_and_a_record_of_the_reads() -> None:
    assert answer(reads=None) is None
    undeclared = ref("shop/Service/elsewhere")
    assert answer(undeclared) is None
    other_role = question(BROKER).model_copy(update={"role": "configuration_source"})
    assert dependency_calls(other_role, LATEST, traffic(), READS, (), ONSET) is None
    assert dependency_calls(question(BROKER), LATEST, traffic(), READS, (), None) is None


def test_in_the_resolution_b_and_c_answer_for_every_claim_and_an_open_one_keeps_a_partial_a() -> (
    None
):
    from test_dependency_frontier import FAULT, POD, claim

    from packages.rca.frontier import DEPENDENCY_ON_CLAIM_PATH_RULE
    from packages.rca.resolution import resolve_hypotheses

    through = claim("leader", FAULT, POD, BROKER, ORDER)
    aside = claim("aside", POD, ORDER)
    partial = question(BROKER).model_copy(update={"affected_entities": (ORDER,)})

    def answers(spans: list[TraceSpanObservation]) -> dict[str, tuple[str, str | None, str]]:
        trace = resolve_hypotheses(
            [through, aside],
            structural_alternatives=[partial, question(CACHE)],
            trace_spans=spans,
            latest=LATEST,
            trace_reads=READS,
            causal_onset=ONSET,
        )
        return {
            a.alternative_id: (a.state, a.blocked_reason, a.rule_id) for a in trace.frontier_answers
        }

    healthy = answers(traffic())
    assert healthy["alternative:broker"] == ("ANSWERED_OBSERVED_HEALTHY", None, B)
    assert healthy["alternative:cache"] == ("ANSWERED_DECLARED_UNUSED", None, C)
    failing = answers(traffic(recent_status=TraceSpanStatus.ERROR))
    assert failing["alternative:broker"] == (
        "OPEN",
        "NOT_ON_EVERY_CLAIM_PATH",
        DEPENDENCY_ON_CLAIM_PATH_RULE,
    )
