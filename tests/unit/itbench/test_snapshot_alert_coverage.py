"""M21 amendment 4: a snapshot's alert-channel observation start is its first capture."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from itbench_builders import snapshot_scenario

from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.source import SnapshotSource


def _source(tmp_path: Path, names: tuple[str, ...]) -> SnapshotSource:
    scenario = snapshot_scenario(tmp_path)
    files = dict(scenario.evidence_files)
    files[ITBenchEvidenceCategory.ALERTS] = names
    return SnapshotSource(scenario.model_copy(update={"evidence_files": files}))


def test_w_is_the_earliest_periodic_or_terminal_capture(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        (
            "alerts/alerts_at_2025-12-15T17-35-49.526451.json",
            "alerts/alerts_at_2025-12-15T17-34-49.528434.json",
            "alerts_in_alerting_state_2025-12-15T175655.720371Z.json",
        ),
    )
    assert source.alert_observation_start() == datetime(
        2025, 12, 15, 17, 34, 49, 528434, tzinfo=UTC
    )


def test_a_single_terminal_capture_is_w(tmp_path: Path) -> None:
    source = _source(tmp_path, ("alerts_in_alerting_state_2025-12-15T175345.105640Z.json",))
    assert source.alert_observation_start() == datetime(
        2025, 12, 15, 17, 53, 45, 105640, tzinfo=UTC
    )


def test_w_never_comes_from_an_alert_active_at(tmp_path: Path) -> None:
    # The fixture's alert file has an activeAt but no capture time in its name.
    scenario = snapshot_scenario(tmp_path)
    source = SnapshotSource(scenario)
    assert source.alerts()  # the alert is read
    assert source.alert_observation_start() is None  # but W is unknown
