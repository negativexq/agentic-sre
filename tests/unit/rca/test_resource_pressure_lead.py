"""m21 §30: resource pressure is a rise of the read's recent part over its lead."""

from __future__ import annotations

from datetime import datetime, timedelta

from test_prometheus_backend import (
    T0,
    _entity,
    _gap,
    _provider_adapter,
    _reader,
    _Response,
    _sample,
    _series,
    _success,
    _Transport,
)

from packages.rca.engine import build_case
from packages.rca.investigation.environment import (
    PrometheusInvestigationBackend,
    SourceInvestigationBackend,
)
from packages.rca.investigation.tools import ResourcePressureTool
from packages.rca.model import (
    RESOURCE_BASELINE_LEAD,
    Alert,
    GapDimension,
    InvestigationQuery,
    ResourcePressure,
)
from packages.rca.signals import resource_findings
from packages.rca.source import InMemorySource

POD = _entity()
REFERENCE = T0
START = REFERENCE - timedelta(minutes=10)


def record(
    *,
    resource: str = "cpu",
    baseline: float | None = 0.1,
    peak: float = 0.9,
    lead: float | None = None,
    recent: float | None = None,
    split: bool = True,
) -> ResourcePressure:
    return ResourcePressure(
        pod=POD,
        container="app",
        resource=resource,
        baseline=baseline,
        peak=peak,
        at=START + timedelta(minutes=1),
        evidence_id=f"prometheus:resource:{resource}:{lead}:{recent}",
        sample_count=40,
        sample_start=START,
        sample_end=REFERENCE + timedelta(minutes=5),
        lead_peak=lead if split else None,
        recent_peak=recent if split else None,
        recent_at=REFERENCE + timedelta(minutes=2) if split and recent is not None else None,
    )


def test_a_rise_of_the_recent_part_over_the_lead_is_a_finding_at_the_recent_maximum() -> None:
    (finding,) = resource_findings([record(lead=0.1, recent=0.8, peak=0.8)])
    assert finding.summary == "app cpu throttling rose from 10% to 80%"
    assert finding.at == REFERENCE + timedelta(minutes=2)
    assert finding.details["peak"] == 0.8


def test_a_burst_in_the_lead_is_no_finding_whatever_the_first_sample() -> None:
    # the first sample (baseline) is low, the lead holds a burst as high as the recent part
    assert resource_findings([record(baseline=0.0, peak=0.9, lead=0.9, recent=0.9)]) == []
    # the lead's maximum must be at most 0.6 of the recent maximum
    assert resource_findings([record(lead=0.55, recent=0.9)]) == []
    assert resource_findings([record(lead=0.54, recent=0.9)])


def test_the_recent_part_must_cross_the_threshold() -> None:
    assert resource_findings([record(lead=0.0, recent=0.24, peak=0.9)]) == []
    assert resource_findings([record(resource="memory", lead=0.1, recent=0.89, peak=0.95)]) == []
    assert resource_findings([record(resource="memory", lead=0.1, recent=0.9)])


def test_no_sample_in_the_lead_is_no_baseline_and_no_finding() -> None:
    assert resource_findings([record(lead=None, recent=0.9)]) == []
    # nothing after the lead is no pressure either
    assert resource_findings([record(lead=0.0, recent=None)]) == []


def test_a_record_read_before_section_30_keeps_the_previous_rule() -> None:
    (finding,) = resource_findings([record(baseline=0.1, peak=0.9, split=False)])
    assert finding.summary == "app cpu throttling rose from 10% to 90%"
    assert finding.at == START + timedelta(minutes=1)
    assert resource_findings([record(baseline=0.7, peak=0.9, split=False)]) == []


def _read(
    values: list[list[object]],
    start: datetime = START,
    end: datetime = REFERENCE + timedelta(minutes=5),
) -> tuple[ResourcePressure, ...]:
    transport = _Transport(lambda url, headers: _Response(_success([_series(values)])))
    return _reader(transport).query_resource_pressure(
        POD, InvestigationQuery(start=start, end=end, limit=16)
    )


def test_the_reader_splits_the_read_at_its_first_baseline_lead() -> None:
    split = START + RESOURCE_BASELINE_LEAD
    records = _read(
        [
            _sample(START, "0.10"),
            _sample(START + timedelta(minutes=2), "0.30"),
            _sample(split - timedelta(seconds=15), "0.20"),
            _sample(split, "0.50"),
            _sample(REFERENCE + timedelta(minutes=1), "0.95"),
            _sample(REFERENCE + timedelta(minutes=2), "0.40"),
        ]
    )
    assert records
    for item in records:
        assert (item.baseline, item.peak) == (0.10, 0.95)
        assert (item.lead_peak, item.recent_peak) == (0.30, 0.95)
        assert item.recent_at == REFERENCE + timedelta(minutes=1)


def test_the_reader_reports_no_lead_when_the_series_begins_after_it() -> None:
    later = START + RESOURCE_BASELINE_LEAD + timedelta(minutes=1)
    for item in _read([_sample(later, "0.10"), _sample(REFERENCE, "0.95")]):
        assert (item.lead_peak, item.recent_peak) == (None, 0.95)
    assert all(not resource_findings([item]) for item in _read([_sample(later, "0.95")]))


def test_an_investigation_tool_read_carries_no_split() -> None:
    source = InMemorySource(
        name="tool-read",
        alert_coverage_start=T0,
        alert_items=[Alert(name="PodPressure", service="payment-service", starts_at=T0)],
    )
    transport = _Transport(
        lambda url, headers: _Response(
            _success([_series([_sample(START, "0.20"), _sample(REFERENCE, "0.95")])])
        )
    )
    backend = PrometheusInvestigationBackend(
        base=SourceInvestigationBackend(source),
        provider_adapter=_provider_adapter(_reader(transport)),
        observation_cutoff=REFERENCE + timedelta(minutes=5),
    )
    observation = ResourcePressureTool(backend).execute_query(
        build_case(source),
        _gap("resource_pressure", GapDimension.RESOURCE_PRESSURE, POD),
        POD,
        InvestigationQuery(start=START, end=REFERENCE + timedelta(minutes=5), limit=32),
    )
    payload = observation.payload["resource_pressure"]
    assert payload
    assert all(item["lead_peak"] is None and item["recent_peak"] is None for item in payload)
