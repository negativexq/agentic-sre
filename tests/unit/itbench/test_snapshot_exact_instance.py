"""M20.5 P2a: the snapshot adapter keeps exact object instances through history compaction.

Same namespace/kind/name does not imply the same object instance; a changed observed
UID must survive history compaction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from itbench_builders import _tsv, snapshot_scenario

from packages.evals.itbench.source import SnapshotSource
from packages.rca.model import EntityRef

POD = EntityRef.parse("shop/Pod/cart-0")


def _pod(uid: str | None, *, image: str = "cart:1", resource_version: str = "1") -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": "cart-0",
        "namespace": "shop",
        "resourceVersion": resource_version,
    }
    if uid is not None:
        metadata["uid"] = uid
    return {"kind": "Pod", "metadata": metadata, "spec": {"containers": [{"image": image}]}}


def _history(tmp_path: Path, rows: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    scenario = snapshot_scenario(tmp_path)
    _tsv(Path(scenario.snapshot_path) / "k8s_objects_raw.tsv", rows)
    return list(SnapshotSource(scenario).object_history()[POD])


def test_a_changed_uid_with_the_same_name_and_spec_is_a_new_version(tmp_path: Path) -> None:
    versions = _history(
        tmp_path,
        [
            ("2025-01-01 12:00:00.000000001", _pod("uid-A")),
            ("2025-01-01 12:05:00.000000001", _pod("uid-B")),
        ],
    )
    assert [version.uid for version in versions] == ["uid-A", "uid-B"]


def test_bookkeeping_metadata_churn_of_one_instance_stays_compacted(tmp_path: Path) -> None:
    versions = _history(
        tmp_path,
        [
            ("2025-01-01 12:00:00.000000001", _pod("uid-A", resource_version="1")),
            ("2025-01-01 12:05:00.000000001", _pod("uid-A", resource_version="2")),
            ("2025-01-01 12:06:00.000000001", _pod("uid-A", resource_version="3")),
        ],
    )
    assert [version.uid for version in versions] == ["uid-A"]


def test_a_missing_uid_is_not_invented(tmp_path: Path) -> None:
    versions = _history(tmp_path, [("2025-01-01 12:00:00.000000001", _pod(None))])
    assert [version.uid for version in versions] == [None]


def test_an_appearing_uid_is_not_assumed_to_be_the_same_instance(tmp_path: Path) -> None:
    versions = _history(
        tmp_path,
        [
            ("2025-01-01 12:00:00.000000001", _pod(None)),
            ("2025-01-01 12:05:00.000000001", _pod("uid-A")),
            ("2025-01-01 12:06:00.000000001", _pod(None)),
        ],
    )
    assert [version.uid for version in versions] == [None, "uid-A", None]


def test_compaction_follows_time_not_file_order(tmp_path: Path) -> None:
    """A rollback (X → Y → X) written out of time order must keep all three versions."""
    versions = _history(
        tmp_path,
        [
            ("2025-01-01 12:00:00.000000001", _pod("uid-A", image="cart:1")),
            ("2025-01-01 12:10:00.000000001", _pod("uid-A", image="cart:1")),
            ("2025-01-01 12:05:00.000000001", _pod("uid-A", image="cart:2")),
        ],
    )
    assert [version.body["spec"]["containers"][0]["image"] for version in versions] == [
        "cart:1",
        "cart:2",
        "cart:1",
    ]
    # Sorting does not renumber evidence: each version keeps its original row index.
    assert [version.evidence_id for version in versions] == [
        "k8s_objects_raw.tsv:0",
        "k8s_objects_raw.tsv:2",
        "k8s_objects_raw.tsv:1",
    ]
    assert [version.observed_at for version in versions] == sorted(
        version.observed_at for version in versions
    )
