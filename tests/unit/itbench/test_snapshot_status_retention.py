"""M20.6: every observed Pod body is a status observation, not only the compacted versions.

History compaction keeps one version per unchanged desired state. Status is what each
observation showed at that moment, so a repeated observation of the same spec must
still reach A1 -- as the live lifecycle ledger's STATUS_SNAPSHOT heartbeat does.
Nothing is inferred for moments without an observed body.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from itbench_builders import _tsv, snapshot_scenario

from packages.evals.itbench.source import SnapshotSource
from packages.rca.episode_end import evaluate_ended_episodes
from packages.rca.model import (
    EntityInstanceRef,
    EntityRef,
    EvidenceTemporalRole,
    Finding,
    FindingKind,
    Hypothesis,
)

POD = EntityRef.parse("shop/Pod/cart-0")
T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
ONSET = T0 + timedelta(minutes=30)
GRACE = timedelta(minutes=5)


def _pod(uid: str, *, ready: str = "True", ready_since: datetime = T0) -> dict[str, Any]:
    return {
        "kind": "Pod",
        "metadata": {"name": "cart-0", "namespace": "shop", "uid": uid},
        "spec": {"containers": [{"image": "cart:1"}]},
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": ready,
                    "lastTransitionTime": ready_since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ]
        },
    }


def _at(minute: int) -> str:
    return (T0 + timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M:%S.000000001")


def _source(tmp_path: Path, rows: list[tuple[str, dict[str, Any]]]) -> SnapshotSource:
    scenario = snapshot_scenario(tmp_path)
    _tsv(Path(scenario.snapshot_path) / "k8s_objects_raw.tsv", rows)
    return SnapshotSource(scenario)


def test_each_repeated_observation_is_a_status_observation(tmp_path: Path) -> None:
    source = _source(tmp_path, [(_at(minute), _pod("uid-A")) for minute in (20, 25, 40)])
    # Compaction is unchanged: one desired-state version.
    assert len(source.object_history()[POD]) == 1
    statuses = [status for status in source.pod_status_observations() if status.pod == POD]
    assert [status.observed_at for status in statuses] == [
        T0 + timedelta(minutes=minute) for minute in (20, 25, 40)
    ]
    # Each keeps its own raw row as evidence, never the compacted version's.
    assert [status.evidence_id for status in statuses] == [
        "k8s_objects_raw.tsv:0",
        "k8s_objects_raw.tsv:1",
        "k8s_objects_raw.tsv:2",
    ]


def test_a_ready_transition_without_a_spec_change_is_not_lost(tmp_path: Path) -> None:
    recovered = T0 + timedelta(minutes=12)
    source = _source(
        tmp_path,
        [
            (_at(10), _pod("uid-A", ready="False", ready_since=T0 + timedelta(minutes=9))),
            (_at(15), _pod("uid-A", ready="True", ready_since=recovered)),
        ],
    )
    assert len(source.object_history()[POD]) == 1
    assert [(status.ready, status.ready_since) for status in source.pod_status_observations()] == [
        (False, T0 + timedelta(minutes=9)),
        (True, recovered),
    ]


def test_status_observations_keep_the_exact_instance(tmp_path: Path) -> None:
    source = _source(tmp_path, [(_at(1), _pod("uid-A")), (_at(2), _pod("uid-B"))])
    assert [status.uid for status in source.pod_status_observations()] == ["uid-A", "uid-B"]


def _manifestation_only(uid: str) -> Hypothesis:
    return Hypothesis(
        hypothesis_id="h-pod",
        causal_actor=POD,
        findings=(
            Finding(
                kind=FindingKind.CONTAINER_FAILURE,
                entity=POD,
                entity_instance=EntityInstanceRef(entity=POD, uid=uid),
                at=T0 - timedelta(days=4),
                summary="container Error (1 restart(s)) exit code 1",
                evidence_ids=("k8s_objects_raw.tsv:0",),
                temporal_role=EvidenceTemporalRole.AMBIGUOUS,
            ),
        ),
    )


def _ended(source: SnapshotSource) -> bool:
    evaluations = evaluate_ended_episodes(
        (_manifestation_only("uid-A"),),
        history=source.object_history(),
        pod_statuses=source.pod_status_observations(),
        onset=ONSET,
        grace=GRACE,
        evaluation_at=ONSET + timedelta(hours=1),
    )
    return "h-pod" in evaluations.ended_episodes


def test_a1_sees_readiness_continuing_past_the_boundary(tmp_path: Path) -> None:
    # Ready since long before onset, observed again at and after onset + grace:
    # the unchanged spec was compacted away before, and A1 saw no data after it.
    rows = [(_at(minute), _pod("uid-A")) for minute in (20, 25, 30, 35, 40)]
    assert _ended(_source(tmp_path, rows))


def test_a1_does_not_end_an_episode_without_an_observation_past_the_boundary(
    tmp_path: Path,
) -> None:
    # Observed only up to onset: continuity through the A1 boundary is unobserved.
    rows = [(_at(minute), _pod("uid-A")) for minute in (20, 25, 30)]
    assert not _ended(_source(tmp_path, rows))


def test_a1_does_not_borrow_another_instances_readiness(tmp_path: Path) -> None:
    rows = [(_at(20), _pod("uid-A"))] + [(_at(minute), _pod("uid-B")) for minute in (36, 40)]
    assert not _ended(_source(tmp_path, rows))
