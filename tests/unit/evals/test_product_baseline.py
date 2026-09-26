"""M19-6.8: the harness turns a dirty baseline into run ERROR, once, before staging."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine
from test_product_runner import ROOT, Commands, _control, _Process

from packages.evals.product.actions import SetReadiness
from packages.evals.product.baseline import DirtyBaseline, check_baseline
from packages.evals.product.live import LiveBackend, LiveEvidenceReader, StageNotImplemented
from packages.evals.product.runner import ProductRunner, RecordingBackend, RunStatus, Stage
from packages.evals.product.spec import Expectation, Phase, ProductScenario
from packages.storage.database import create_session_factory
from packages.storage.models import Base

CLEAN = {
    "initiating_finding_count": 0,
    "initiating_finding_ids": [],
    "root_eligible_manifestation_only_count": 0,
    "root_eligible_manifestation_only_hypothesis_ids": [],
}


def _doc(**overrides: Any) -> dict[str, Any]:
    return {**CLEAN, **overrides}


def test_a_clean_evaluation_passes() -> None:
    check_baseline(CLEAN)


@pytest.mark.parametrize(
    "document",
    [
        _doc(initiating_finding_count=1, initiating_finding_ids=["SPEC_CHANGE|x|journal:2"]),
        _doc(
            root_eligible_manifestation_only_count=1,
            root_eligible_manifestation_only_hypothesis_ids=["hypothesis:abc"],
        ),
    ],
    ids=["initiating", "manifestation-only"],
)
def test_either_candidate_kind_is_a_dirty_baseline(document: dict[str, Any]) -> None:
    with pytest.raises(DirtyBaseline, match="dirty baseline"):
        check_baseline(document)


@pytest.mark.parametrize(
    "document",
    [
        {},
        _doc(initiating_finding_count=1),  # count without ids
        _doc(initiating_finding_ids=["x"]),  # ids without count
        _doc(initiating_finding_count=True),
    ],
)
def test_a_malformed_evaluation_fails_loudly(document: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        check_baseline(document)


class DirtyRecording(RecordingBackend):
    def clean_baseline(self, scenario: ProductScenario) -> None:
        super().clean_baseline(scenario)
        raise DirtyBaseline(("SPEC_CHANGE|x|journal:2",), ())


def test_a_dirty_baseline_is_error_before_the_timeline_with_teardown() -> None:
    backend = DirtyRecording()
    scenario = ProductScenario(
        scenario_id="s",
        phases=(Phase(timedelta(0), (SetReadiness("order-service", True, timedelta(1)),)),),
        expectation=Expectation(),
    )
    result = ProductRunner(backend).run(scenario)
    assert result.status is RunStatus.ERROR
    assert result.error is not None and "dirty baseline" in result.error
    stages = list(backend.stages())
    assert stages[-2:] == [Stage.CLEAN_BASELINE, Stage.CLUSTER_DOWN]
    assert Stage.TIMELINE not in stages
    assert stages.count(Stage.CLEAN_BASELINE) == 1  # one attempt, never retried
    assert not any(event[0] == "set_not_ready" for event in backend.events)


def _backend(document: Mapping[str, Any], requests: list[tuple[str, Any, Any]]) -> LiveBackend:
    def request_json(
        url: str, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> Mapping[str, Any]:
        requests.append((url, dict(payload), dict(headers)))
        return document

    def spawn(argv: Sequence[str], env: Mapping[str, str]) -> _Process:
        return _Process()

    evidence_engine = create_engine("sqlite://")
    Base.metadata.create_all(evidence_engine)
    return LiveBackend(
        root=ROOT,
        control_port=_control(Commands(), []),
        evidence_port=LiveEvidenceReader(create_session_factory(evidence_engine)),
        run=Commands(),
        spawn=spawn,
        sleep=lambda _: None,
        control_plane_url="http://cp",
        api_token="t0ken",
        request_json=request_json,
    )


def test_the_live_probe_window_starts_before_the_control_plane_and_ends_at_the_check() -> None:
    requests: list[tuple[str, Any, Any]] = []
    backend = _backend(CLEAN, requests)
    with pytest.raises(StageNotImplemented, match="M19-6.12"):
        ProductRunner(backend).run(
            ProductScenario(scenario_id="s", phases=(), expectation=Expectation())
        )
    ((url, payload, headers),) = requests
    assert url == "http://cp/api/v1/baseline-probe"
    assert headers == {"Authorization": "Bearer t0ken"}
    assert payload["namespace"] == "sre-demo"
    assert payload["collector_started_at"] <= payload["baseline_reference_at"]


def test_the_live_probe_maps_a_dirty_answer_to_error() -> None:
    requests: list[tuple[str, Any, Any]] = []
    dirty = _doc(
        root_eligible_manifestation_only_count=1,
        root_eligible_manifestation_only_hypothesis_ids=["hypothesis:abc"],
    )
    result = ProductRunner(_backend(dirty, requests)).run(
        ProductScenario(scenario_id="s", phases=(), expectation=Expectation())
    )
    assert result.status is RunStatus.ERROR and len(requests) == 1


def test_the_probe_needs_a_started_control_plane() -> None:
    backend = _backend(CLEAN, [])
    with pytest.raises(RuntimeError, match="not been started"):
        backend.clean_baseline(
            ProductScenario(scenario_id="s", phases=(), expectation=Expectation())
        )
