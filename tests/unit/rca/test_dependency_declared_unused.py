"""m21 §29 C: a dependency declared and never used by its traced caller (§29.6); B is withdrawn (§29.9)."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import count

from rca_builders import at, microservice, ref

from packages.rca.evidence_coverage import TraceRead
from packages.rca.frontier import DEPENDENCY_DECLARED_UNUSED_RULE, dependency_declared_unused
from packages.rca.model import (
    EntityRef,
    FrontierAnswer,
    ObjectVersion,
    StructuralAlternative,
    TraceSpanObservation,
)

ONSET = at(60)
ORDER = ref("shop/Deployment/order")
BROKER = ref("shop/Service/broker")
CACHE = ref("shop/Service/cache")
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
C = DEPENDENCY_DECLARED_UNUSED_RULE


def span(
    minutes: float,
    *,
    kind: str = "PRODUCER",
    address: str | None = "broker",
    service: str = "order",
) -> TraceSpanObservation:
    start: datetime = at(minutes)
    n = next(_ids)
    return TraceSpanObservation(
        trace_id=f"t{n}",
        span_id=f"s{n}",
        service=service,
        span_name="publish",
        span_kind=kind,
        start_at=start,
        end_at=start + timedelta(seconds=0.01),
        semantic_attributes={"server.address": address} if address else {},
        evidence_id=f"span:{n}",
    )


def traffic() -> list[TraceSpanObservation]:
    """Server spans of the caller, healthy broker calls up to the onset and a database call."""
    return [
        *(span(m, kind="SERVER", address=None) for m in (40, 50, 58)),
        *(span(m) for m in (40, 45, 50, 59.2, 59.5, 59.8)),
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
    actor: EntityRef = CACHE,
    spans: list[TraceSpanObservation] | None = None,
    *,
    reads: list[TraceRead] | None = READS,
) -> FrontierAnswer | None:
    return dependency_declared_unused(
        question(actor), LATEST, traffic() if spans is None else spans, reads, ONSET
    )


def state(found: FrontierAnswer | None) -> tuple[str, str | None, str] | None:
    return None if found is None else (found.state, found.blocked_reason, found.rule_id)


def test_a_declaration_the_traced_caller_never_used_answers_the_question() -> None:
    found = answer()
    assert state(found) == ("ANSWERED_DECLARED_UNUSED", None, C)
    assert found is not None and found.evidence_ids
    assert found.affected_claims == ("leader",)


def test_healthy_calls_answer_nothing_since_b_is_withdrawn() -> None:
    # m21 §29.9: two DEV checks showed calls made before a dependency's fault read as healthy
    assert state(answer(BROKER)) == ("OPEN", "CALLED", C)


def test_each_condition_of_c_keeps_the_question_open() -> None:
    no_server = [s for s in traffic() if s.span_kind != "SERVER"]
    assert state(answer(spans=no_server)) == ("OPEN", "CALLER_NOT_TRACED", C)
    unattributed = [*traffic(), span(58, kind="INTERNAL", address=None)]
    assert state(answer(spans=unattributed)) == ("OPEN", "CALLS_NOT_TRACED", C)
    no_outbound = [s for s in traffic() if s.span_kind == "SERVER"]
    assert state(answer(spans=no_outbound)) == ("OPEN", "CALLS_NOT_TRACED", C)
    failed_read = [READS[0].model_copy(update={"completeness": "FAILED"})]
    assert state(answer(reads=failed_read)) == ("OPEN", "TRACE_READ_FAILED", C)
    called = [*traffic(), span(30, kind="CLIENT", address="cache")]
    assert state(answer(spans=called)) == ("OPEN", "CALLED", C)


def test_a_cut_read_is_not_a_failed_read() -> None:
    cut = [READS[0].model_copy(update={"completeness": "TRUNCATED"})]
    assert state(answer(reads=cut)) == ("ANSWERED_DECLARED_UNUSED", None, C)


def test_only_server_address_attributes_a_call() -> None:
    # a span that would name the cache by its name, kind or service carries no declared host: not a call to it
    named = [*traffic(), span(55, kind="CLIENT", address="cache-2")]
    assert state(answer(spans=named)) == ("ANSWERED_DECLARED_UNUSED", None, C)
    callee_side = [*traffic(), span(55, service="cache", kind="SERVER", address=None)]
    assert state(answer(spans=callee_side)) == ("ANSWERED_DECLARED_UNUSED", None, C)
    # an outgoing span without a peer leaves the calls not traced, never "unused"
    unnamed = [
        s.model_copy(update={"semantic_attributes": {}}) if s.span_kind == "PRODUCER" else s
        for s in traffic()
    ]
    assert state(answer(spans=unnamed)) == ("OPEN", "CALLS_NOT_TRACED", C)


def test_a_call_after_the_onset_is_outside_the_window() -> None:
    after = [*traffic(), span(61, kind="CLIENT", address="cache")]
    assert state(answer(spans=after)) == ("ANSWERED_DECLARED_UNUSED", None, C)


def test_the_rule_needs_a_dependency_question_a_declaration_and_a_record_of_the_reads() -> None:
    assert answer(reads=None) is None
    assert answer(ref("shop/Service/elsewhere")) is None
    other_role = question(CACHE).model_copy(update={"role": "configuration_source"})
    assert dependency_declared_unused(other_role, LATEST, traffic(), READS, ONSET) is None
    assert dependency_declared_unused(question(CACHE), LATEST, traffic(), READS, None) is None


def test_in_the_resolution_c_answers_for_every_claim_and_an_open_one_keeps_a_partial_a() -> None:
    from test_dependency_frontier import FAULT, POD, claim

    from packages.rca.frontier import DEPENDENCY_ON_CLAIM_PATH_RULE
    from packages.rca.resolution import resolve_hypotheses

    through = claim("leader", FAULT, POD, BROKER, ORDER)
    aside = claim("aside", POD, ORDER)
    trace = resolve_hypotheses(
        [through, aside],
        structural_alternatives=[question(BROKER), question(CACHE)],
        trace_spans=traffic(),
        latest=LATEST,
        trace_reads=READS,
        causal_onset=ONSET,
    )
    found = {
        a.alternative_id: (a.state, a.blocked_reason, a.rule_id) for a in trace.frontier_answers
    }
    assert found["alternative:cache"] == ("ANSWERED_DECLARED_UNUSED", None, C)
    assert found["alternative:broker"] == (
        "OPEN",
        "NOT_ON_EVERY_CLAIM_PATH",
        DEPENDENCY_ON_CLAIM_PATH_RULE,
    )
