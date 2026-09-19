import csv
from datetime import UTC, datetime
from pathlib import Path

from packages.evals.itbench.contracts import ITBenchEvidenceCategory, ITBenchScenario
from packages.evals.itbench.source import SnapshotSource
from packages.rca.investigation.environment import (
    InitialObservationView,
    initial_view,
    investigation_backend,
)
from packages.rca.model import TraceSpanObservation
from packages.rca.source import InMemorySource


def _scenario(root: Path) -> ITBenchScenario:
    return ITBenchScenario(
        scenario_id="Scenario-900",
        snapshot_path=str(root),
        evidence_categories=(ITBenchEvidenceCategory.TRACES,),
        evidence_files={ITBenchEvidenceCategory.TRACES: ("otel_traces_raw.tsv",)},
        observation_end="2025-01-01T00:00:03Z",
    )


def test_snapshot_source_ingests_ordered_valid_traces_and_applies_cutoff(tmp_path: Path) -> None:
    path = tmp_path / "otel_traces_raw.tsv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(
            ["Timestamp", "TraceId", "SpanId", "ParentSpanId", "ServiceName", "SpanName"]
        )
        writer.writerow(["2025-01-01T00:00:01Z", "T", "ROOT", "", "frontend", "root"])
        writer.writerow(["2025-01-01T00:00:02Z", "T", "CHILD", "ROOT", "checkout", "child"])
        writer.writerow(["2025-01-01T00:00:02Z", "", "BAD", "ROOT", "bad", "invalid"])
        writer.writerow(["2025-01-01T00:00:04Z", "T", "LATE", "ROOT", "late", "hidden"])

    observations = SnapshotSource(_scenario(tmp_path)).trace_observations()
    assert [item.span_id for item in observations] == ["root", "child"]
    assert [item.evidence_id for item in observations] == [
        "otel_traces_raw.tsv:0",
        "otel_traces_raw.tsv:1",
    ]
    assert observations[0].parent_span_id is None
    assert observations[1].parent_span_id == "root"


def test_initial_view_hides_traces_and_backend_has_no_trace_capability() -> None:
    item = TraceSpanObservation(
        trace_id="t",
        span_id="s",
        service="checkout",
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        evidence_id="trace:0",
    )
    source = InMemorySource(name="trace-test", trace_items=[item])
    bounded = initial_view(source)
    assert bounded.trace_observations() == ()
    assert isinstance(bounded, InitialObservationView)
    assert bounded.access_ledger()["initial_trace_refs"] == ()
    assert investigation_backend(source).supports("traces") is False


def test_in_memory_trace_cutoff_includes_boundary_and_excludes_later_span() -> None:
    cutoff = datetime(2025, 1, 1, 0, 0, 2, tzinfo=UTC)
    items = [
        TraceSpanObservation(
            trace_id="t",
            span_id="at-boundary",
            service="checkout",
            start_at=cutoff,
            evidence_id="trace:0",
        ),
        TraceSpanObservation(
            trace_id="t",
            span_id="after-boundary",
            service="checkout",
            start_at=datetime(2025, 1, 1, 0, 0, 3, tzinfo=UTC),
            evidence_id="trace:1",
        ),
    ]
    source = InMemorySource(name="cutoff-test", trace_items=items, cutoff=cutoff)
    assert [item.span_id for item in source.trace_observations()] == ["at-boundary"]
