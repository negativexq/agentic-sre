"""The per-scope evidence coverage record of a diagnosis (late-evidence-design.md §4).

Provenance kept with the diagnosis, outside the epistemic digest: no rule reads it until a rule change of §5
is approved. Two dimensions are kept apart (§4.3); the summary is derived, never stored.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class SourceContinuity(StrEnum):
    CONTINUOUS = "CONTINUOUS"
    GAPPED = "GAPPED"
    # no stream, or the stream was not followed for the whole window: legacy behaviour, recorded as such
    UNKNOWN = "UNKNOWN"


class TransportCompleteness(StrEnum):
    PROVEN = "PROVEN"
    NOT_PROVEN = "NOT_PROVEN"  # the wait for the stream to pass the window's end timed out
    NOT_APPLICABLE = "NOT_APPLICABLE"  # no stream


class CoverageSummary(StrEnum):
    COVERED = "COVERED"
    NOT_COVERED = "NOT_COVERED"


class StreamGap(BaseModel):
    """A persisted change-stream gap; no scope means every scope lost continuity."""

    model_config = ConfigDict(frozen=True)

    reason: str
    since: datetime | None  # unknown start: it may reach back to any time before ``at``
    at: datetime
    namespace: str | None = None
    kind: str | None = None

    def covers(self, namespace: str, kind: str) -> bool:
        return self.namespace is None or (self.namespace, self.kind) == (namespace, kind)


class ScopeCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    namespace: str
    kind: str
    source_continuity: SourceContinuity
    gaps: tuple[StreamGap, ...] = ()
    transport_completeness: TransportCompleteness
    transport_proven_at: datetime | None = None

    def summary(self) -> tuple[CoverageSummary, tuple[str, ...]]:
        """Derived for the console and reports, never read by a rule: the verdict and the failed dimensions."""
        failed = tuple(
            name
            for name, positive in (
                ("source_continuity", self.source_continuity is SourceContinuity.CONTINUOUS),
                (
                    "transport_completeness",
                    self.transport_completeness is TransportCompleteness.PROVEN,
                ),
            )
            if not positive
        )
        return (CoverageSummary.NOT_COVERED if failed else CoverageSummary.COVERED), failed


class EvidenceCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    starts_at: datetime
    window_end: datetime
    # the first Connector time this control plane read from the stream; continuity before it is unknown
    stream_followed_since: datetime | None = None
    scopes: tuple[ScopeCoverage, ...] = ()


def evidence_coverage(
    scopes: Iterable[tuple[str, str]],
    gaps: Iterable[StreamGap],
    *,
    starts_at: datetime,
    window_end: datetime,
    streamed: bool,
    stream_followed_since: datetime | None,
    transport_proven_at: datetime | None,
) -> EvidenceCoverage:
    """Record, for each scope, whether it was observed continuously and whether transport is proven.

    ``gaps`` are the persisted gaps overlapping ``[starts_at, window_end]``.
    """
    gaps = tuple(gaps)
    wanted = set(scopes) | {(g.namespace, g.kind) for g in gaps if g.namespace and g.kind}
    followed = streamed and stream_followed_since is not None and stream_followed_since <= starts_at
    if not streamed:
        transport = TransportCompleteness.NOT_APPLICABLE
    elif transport_proven_at is not None:
        transport = TransportCompleteness.PROVEN
    else:
        transport = TransportCompleteness.NOT_PROVEN
    records = []
    for namespace, kind in sorted(wanted):
        own = tuple(g for g in gaps if g.covers(namespace, kind)) if streamed else ()
        if own:
            continuity = SourceContinuity.GAPPED  # a known gap is a gap, whatever else is unknown
        elif followed:
            continuity = SourceContinuity.CONTINUOUS
        else:
            continuity = SourceContinuity.UNKNOWN
        records.append(
            ScopeCoverage(
                namespace=namespace,
                kind=kind,
                source_continuity=continuity,
                gaps=own,
                transport_completeness=transport,
                transport_proven_at=transport_proven_at
                if transport is TransportCompleteness.PROVEN
                else None,
            )
        )
    return EvidenceCoverage(
        starts_at=starts_at,
        window_end=window_end,
        stream_followed_since=stream_followed_since if streamed else None,
        scopes=tuple(records),
    )


def continuously_observed(
    coverage: EvidenceCoverage, namespace: str, kind: str, start: datetime, end: datetime
) -> str | None:
    """Why the scope was not shown observed continuously over ``[start, end]``, or None when it was (m21 §26.6).

    The stream was followed since ``start`` or earlier, no gap of the scope overlaps the interval, and transport is
    proven. Read from the record frozen at the run boundary.
    """
    scope = next((s for s in coverage.scopes if (s.namespace, s.kind) == (namespace, kind)), None)
    if scope is None:
        return "SCOPE_NOT_RECORDED"
    if scope.transport_completeness is not TransportCompleteness.PROVEN:
        return "TRANSPORT_NOT_PROVEN"
    if coverage.stream_followed_since is None or _utc(coverage.stream_followed_since) > start:
        return "SCOPE_NOT_CONTINUOUS"
    if any(
        (gap.since is None or _utc(gap.since) <= end) and _utc(gap.at) >= start
        for gap in scope.gaps
    ):
        return "SCOPE_NOT_CONTINUOUS"
    return None


def _utc(instant: datetime) -> datetime:
    """Gap rows are stored without a zone, in UTC."""
    return instant if instant.tzinfo is not None else instant.replace(tzinfo=UTC)
