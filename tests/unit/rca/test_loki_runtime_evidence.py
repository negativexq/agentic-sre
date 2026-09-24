from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.request import Request

import pytest
from rca_builders import alert, microservice

from packages.rca.engine import Case, build_case
from packages.rca.investigation.environment import (
    LokiInvestigationBackend,
    SourceInvestigationBackend,
    initial_view,
)
from packages.rca.investigation.normalizers import normalize_observation
from packages.rca.investigation.tools import LogsTool
from packages.rca.live import LokiLogReader
from packages.rca.model import (
    AuthorizedQuery,
    EntityRef,
    FindingKind,
    GapDimension,
    GapOutcomeKind,
    Hypothesis,
    InformationGap,
    InvestigationQuery,
    LogRecord,
    RuntimeEvidencePillar,
    RuntimeObservationState,
)
from packages.rca.source import InMemorySource

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class _Loki:
    def __init__(self, records: Sequence[LogRecord]) -> None:
        self.records = list(records)
        self.calls: list[tuple[tuple[str, ...], datetime, datetime, int | None]] = []

    def error_logs(
        self,
        services: Sequence[str],
        starts_at: datetime,
        ends_at: datetime,
        *,
        limit: int | None = None,
    ) -> list[LogRecord]:
        self.calls.append((tuple(services), starts_at, ends_at, limit))
        return self.records[:limit]


def _fixture(
    records: Sequence[LogRecord],
) -> tuple[Case, InformationGap, EntityRef, LokiInvestigationBackend]:
    caller = EntityRef(namespace="shop", kind="Deployment", name="caller")
    source = InMemorySource(
        name="loki-runtime",
        alert_items=[alert("latency", "caller", 0)],
        versions=[
            *microservice(
                "caller",
                0,
                {"A_ADDR": "backend-a:1", "B_ADDR": "backend-b:1", "C_ADDR": "backend-c:1"},
            ),
            *microservice("backend-a", 0),
            *microservice("backend-b", 0),
            *microservice("backend-c", 0),
        ],
        cutoff=T0 + timedelta(minutes=30),
    )
    case = build_case(initial_view(source))
    gap = InformationGap(
        gap_id="gap:dependency-health",
        dimension=GapDimension.DEPENDENCY_HEALTH,
        missing_fact="bounded dependency error observation",
        authorized_queries=(AuthorizedQuery(capability="logs", target=caller),),
    )
    backend = LokiInvestigationBackend(
        base=SourceInvestigationBackend(source),
        loki=_Loki(records),
        source=source,
        observation_cutoff=source.observation_cutoff(),
    )
    return case, gap, caller, backend


def _query() -> InvestigationQuery:
    return InvestigationQuery(start=T0, end=T0 + timedelta(minutes=10), limit=64)


def test_loki_runtime_path_normalizes_dependency_finding_with_provenance() -> None:
    records = [
        LogRecord(
            service="caller",
            at=T0 + timedelta(minutes=1),
            severity="ERROR",
            message="backend-c connection refused",
            evidence_id="loki:caller:1",
        )
    ]
    case, gap, caller, backend = _fixture(records)
    observation = LogsTool(backend).execute_query(case, gap, caller, _query())

    assert observation.runtime is not None
    assert observation.runtime.pillar is RuntimeEvidencePillar.LOKI
    assert observation.runtime.state is RuntimeObservationState.UNKNOWN
    assert observation.runtime.query.limit == 32
    assert observation.runtime.query.template_id == "loki.error_logs.v1"

    normalized = normalize_observation(observation, case=case, gap=gap)
    assert [finding.kind for finding in normalized.findings] == [FindingKind.DEPENDENCY_ERRORS]
    assert normalized.observation.runtime is not None
    assert normalized.observation.runtime.state is RuntimeObservationState.OBSERVED_ABNORMAL
    details = normalized.findings[0].details
    assert details["runtime_pillar"] == "LOKI"
    assert details["runtime_query_template_id"] == "loki.error_logs.v1"
    assert details["runtime_source_observation_ids"] == ("loki:caller:1",)
    assert details["runtime_normalization_rule_id"] == "loki.dependency_error_pattern.v1"
    assert "LogQL" not in str(details)

    reader = backend.loki
    assert isinstance(reader, _Loki)
    assert reader.calls[0][3] == 32
    assert reader.calls[0][1:] == (T0, T0 + timedelta(minutes=10), 32)

    another_window = LogsTool(backend).execute_query(
        case,
        gap,
        caller,
        InvestigationQuery(
            start=T0 + timedelta(seconds=30), end=T0 + timedelta(minutes=10), limit=32
        ),
    )
    assert another_window.runtime is not None
    assert another_window.runtime.query.descriptor_id != observation.runtime.query.descriptor_id


def test_loki_hypothesis_attribution_uses_normalized_dependency_actors() -> None:
    records = (
        LogRecord(
            service="caller",
            at=T0 + timedelta(minutes=1),
            severity="ERROR",
            message="backend-c connection refused",
            evidence_id="loki:caller:attribution",
        ),
    )
    case, gap, caller, backend = _fixture(records)
    observation = LogsTool(backend).execute_query(case, gap, caller, _query())
    first = normalize_observation(observation, case=case, gap=gap)
    assert len(first.findings) == 1
    finding = first.findings[0]
    caller_actor = EntityRef.parse(finding.details["caller"])
    dependency_actor = finding.entity
    unrelated_target = EntityRef(namespace="shop", kind="Deployment", name="different-query")
    case = replace(
        case,
        hypotheses=[
            Hypothesis(hypothesis_id="h-query-only", causal_actor=unrelated_target),
            Hypothesis(hypothesis_id="h-caller", causal_actor=caller_actor),
            Hypothesis(hypothesis_id="h-dependency", causal_actor=dependency_actor),
        ],
    )

    normalized = normalize_observation(observation, case=case, gap=gap)

    assert normalized.observation.hypothesis_ids == ("h-caller", "h-dependency")


def test_loki_no_data_and_unrecognized_text_remain_neutral() -> None:
    empty_case, gap, caller, empty_backend = _fixture(())
    empty = LogsTool(empty_backend).execute_query(empty_case, gap, caller, _query())
    assert empty.outcome is GapOutcomeKind.NO_DATA
    assert empty.runtime is not None
    assert empty.runtime.state is RuntimeObservationState.NO_DATA
    assert normalize_observation(empty, case=empty_case, gap=gap).findings == ()

    unknown_record = LogRecord(
        service="caller",
        at=T0 + timedelta(minutes=1),
        severity="INFO",
        message="request completed with ordinary response",
        evidence_id="loki:caller:ordinary",
    )
    case, gap, caller, backend = _fixture((unknown_record,))
    observation = LogsTool(backend).execute_query(case, gap, caller, _query())
    normalized = normalize_observation(observation, case=case, gap=gap)
    assert normalized.findings == ()
    assert normalized.observation.runtime is not None
    assert normalized.observation.runtime.state is RuntimeObservationState.UNKNOWN
    assert normalized.observation.outcome is GapOutcomeKind.UNKNOWN


def test_loki_backend_rejects_unbounded_or_oversized_windows_before_reader_call() -> None:
    case, gap, caller, backend = _fixture(())
    with pytest.raises(ValueError, match="<= 3600s"):
        backend.query_logs(
            caller,
            InvestigationQuery(start=T0, end=T0 + timedelta(hours=2), limit=8),
        )
    assert isinstance(backend.loki, _Loki)
    assert backend.loki.calls == []


def test_loki_reader_explicit_limit_is_capped_and_window_is_bounded() -> None:
    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            del limit
            return b'{"data":{"result":[]}}'

    captured: dict[str, object] = {}

    def opener(request: Request, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response()

    reader = LokiLogReader("http://loki:3100", opener=opener)
    assert reader.error_logs(["caller"], T0, T0 + timedelta(minutes=5), limit=64) == []
    assert "limit=32" in str(captured["url"])
    assert captured["timeout"] == 5.0
    with pytest.raises(ValueError, match="<= 3600 seconds"):
        reader.error_logs(["caller"], T0, T0 + timedelta(hours=2))
    with pytest.raises(ValueError, match="timeout must be between"):
        LokiLogReader("http://loki:3100", timeout_seconds=31)
