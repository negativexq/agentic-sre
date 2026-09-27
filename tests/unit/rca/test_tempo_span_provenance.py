"""M20.5b: Tempo provenance the provider reports is preserved, never interpreted."""

from __future__ import annotations

from typing import Any

import pytest
from test_tempo_backend import TRACE_ID, _attribute, _raw_span, _trace_payload

from packages.rca.investigation.tempo import (
    MAX_SPAN_LINKS,
    TempoProtocolError,
    parse_tempo_trace_json,
)
from packages.rca.model import TraceSpanLink
from packages.rca.runtime_graph import derive_runtime_graph

OTHER_TRACE = "c" * 32


def _link(span_id: str, trace_id: str = OTHER_TRACE, **attributes: Any) -> dict[str, object]:
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "traceState": "vendor=1",
        "attributes": [_attribute(key, value) for key, value in attributes.items()],
    }


def _span_with(**extra: Any) -> dict[str, object]:
    raw = _raw_span("1" * 16, kind=5)
    raw.update(extra)
    return raw


def _parse(raw: dict[str, object]) -> Any:
    (span,) = parse_tempo_trace_json(_trace_payload([raw]))
    return span


def test_exact_messaging_and_pod_uid_keys_are_preserved() -> None:
    span = _parse(
        _raw_span(
            "1" * 16,
            kind=5,
            attributes=[
                _attribute("k8s.pod.uid", "0f1e2d3c-aaaa-bbbb-cccc-000000000001"),
                _attribute("messaging.system", "kafka"),
                _attribute("messaging.destination.name", "orders"),
                _attribute("messaging.operation", "receive"),
                _attribute("messaging.operation.type", "receive"),
                _attribute("messaging.operation.name", "poll"),
                _attribute("messaging.message.id", "m-1"),
                _attribute("messaging.message.conversation_id", "c-1"),
                # Not allowlisted: no wildcard messaging parser.
                _attribute("messaging.kafka.message.key", "k-1"),
                _attribute("messaging.custom", "x"),
            ],
        )
    )
    attrs = span.semantic_attributes
    assert attrs["k8s.pod.uid"] == "0f1e2d3c-aaaa-bbbb-cccc-000000000001"
    assert {
        key: attrs[key]
        for key in (
            "messaging.system",
            "messaging.destination.name",
            "messaging.operation",
            "messaging.operation.type",
            "messaging.operation.name",
            "messaging.message.id",
            "messaging.message.conversation_id",
        )
    } == {
        "messaging.system": "kafka",
        "messaging.destination.name": "orders",
        "messaging.operation": "receive",
        "messaging.operation.type": "receive",
        "messaging.operation.name": "poll",
        "messaging.message.id": "m-1",
        "messaging.message.conversation_id": "c-1",
    }
    assert "messaging.kafka.message.key" not in attrs
    assert "messaging.custom" not in attrs


def test_a_span_without_links_has_none() -> None:
    span = _parse(_raw_span("1" * 16))
    assert span.links == ()
    assert span.dropped_link_count == 0


def test_links_are_typed_ordered_and_keep_only_allowlisted_attributes() -> None:
    span = _parse(
        _span_with(
            links=[
                _link(
                    "3" * 16,
                    **{
                        "messaging.message.id": "m-2",
                        "messaging.kafka.offset": "7",
                        "vendor.secret": "s",
                    },
                ),
                _link("2" * 16, trace_id=TRACE_ID),
            ]
        )
    )
    assert span.links == (
        TraceSpanLink(trace_id=TRACE_ID, span_id="2" * 16),
        TraceSpanLink(
            trace_id=OTHER_TRACE,
            span_id="3" * 16,
            semantic_attributes={"messaging.message.id": "m-2"},
        ),
    )


def test_identical_duplicate_links_collapse() -> None:
    span = _parse(_span_with(links=[_link("2" * 16), _link("2" * 16)]))
    assert span.links == (TraceSpanLink(trace_id=OTHER_TRACE, span_id="2" * 16),)


def test_conflicting_duplicate_links_fail_closed() -> None:
    raw = _span_with(
        links=[
            _link("2" * 16, **{"messaging.message.id": "m-1"}),
            _link("2" * 16, **{"messaging.message.id": "m-2"}),
        ]
    )
    with pytest.raises(TempoProtocolError, match="conflicting duplicate span links"):
        _parse(raw)


def test_links_are_bounded_and_the_cut_is_recorded() -> None:
    links = [_link(f"{index:016x}") for index in range(1, MAX_SPAN_LINKS + 4)]
    span = _parse(_span_with(links=list(reversed(links))))
    assert len(span.links) == MAX_SPAN_LINKS
    assert [link.span_id for link in span.links] == [
        f"{index:016x}" for index in range(1, MAX_SPAN_LINKS + 1)
    ]
    assert span.dropped_link_count == 3


def test_links_create_no_runtime_relation() -> None:
    """Preservation only: a link is not parentage, not a runtime edge."""
    producer = _raw_span("1" * 16, kind=4, trace_id=OTHER_TRACE)
    consumer = _raw_span("2" * 16, kind=5)
    linked = dict(consumer, links=[_link("1" * 16)])
    without = parse_tempo_trace_json(_trace_payload([consumer])) + parse_tempo_trace_json(
        _trace_payload([producer], service="producer")
    )
    with_links = parse_tempo_trace_json(_trace_payload([linked])) + parse_tempo_trace_json(
        _trace_payload([producer], service="producer")
    )
    assert any(span.links for span in with_links)

    def shape(spans: Any) -> Any:
        graph = derive_runtime_graph(spans)
        return graph.stats, graph.edges

    assert shape(with_links) == shape(without)
