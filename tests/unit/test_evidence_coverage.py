"""The evidence coverage record (late-evidence-design.md §4): two dimensions per scope, summary derived."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.rca.evidence_coverage import (
    CoverageSummary,
    ScopeCoverage,
    SourceContinuity,
    StreamGap,
    TransportCompleteness,
    evidence_coverage,
)

T = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
START = T - timedelta(hours=1)
PROVEN_AT = T + timedelta(seconds=3)


def minutes(n: float) -> datetime:
    return T + timedelta(minutes=n)


def record(
    *,
    gaps: list[StreamGap] | None = None,
    streamed: bool = True,
    stream_followed_since: datetime | None = START - timedelta(hours=1),
    transport_proven_at: datetime | None = PROVEN_AT,
) -> dict[tuple[str, str], ScopeCoverage]:
    coverage = evidence_coverage(
        [("shop", "Event"), ("shop", "Pod")],
        gaps or [],
        starts_at=START,
        window_end=T,
        streamed=streamed,
        stream_followed_since=stream_followed_since,
        transport_proven_at=transport_proven_at,
    )
    return {(s.namespace, s.kind): s for s in coverage.scopes}


def test_a_scope_without_a_gap_and_with_transport_proven_is_covered() -> None:
    scope = record()[("shop", "Event")]
    assert scope.source_continuity is SourceContinuity.CONTINUOUS
    assert scope.transport_completeness is TransportCompleteness.PROVEN
    assert scope.summary() == (CoverageSummary.COVERED, ())


def test_a_gap_of_one_scope_leaves_the_others_continuous_and_names_its_interval() -> None:
    gap = StreamGap(
        reason="RESOURCE_VERSION_EXPIRED",
        since=minutes(-12),
        at=minutes(-10),
        namespace="shop",
        kind="Event",
    )
    scopes = record(gaps=[gap])
    assert scopes[("shop", "Event")].gaps == (gap,)
    assert scopes[("shop", "Event")].source_continuity is SourceContinuity.GAPPED
    assert scopes[("shop", "Pod")].source_continuity is SourceContinuity.CONTINUOUS


def test_continuous_source_with_transport_not_proven_is_not_covered_for_that_reason_alone() -> None:
    scope = record(transport_proven_at=None)[("shop", "Pod")]
    assert scope.source_continuity is SourceContinuity.CONTINUOUS
    assert scope.summary() == (CoverageSummary.NOT_COVERED, ("transport_completeness",))


def test_a_global_gap_reaches_every_scope() -> None:
    gap = StreamGap(reason="CONNECTOR_RESTART", since=None, at=minutes(-5))
    scopes = record(gaps=[gap])
    assert {s.source_continuity for s in scopes.values()} == {SourceContinuity.GAPPED}


def test_without_a_stream_continuity_is_unknown_and_transport_not_applicable() -> None:
    scope = record(streamed=False, stream_followed_since=None, transport_proven_at=None)[
        ("shop", "Event")
    ]
    assert scope.source_continuity is SourceContinuity.UNKNOWN
    assert scope.transport_completeness is TransportCompleteness.NOT_APPLICABLE


def test_a_stream_followed_only_after_the_window_started_does_not_claim_continuity() -> None:
    scope = record(stream_followed_since=START + timedelta(minutes=1))[("shop", "Pod")]
    assert scope.source_continuity is SourceContinuity.UNKNOWN
