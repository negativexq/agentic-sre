"""The 2.12.0 §23 replica predicate, frozen for the paired recalculation of m21 §34."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from packages.rca.model import PodStatusObservation


def _workload(pod: str) -> str:
    return pod.rsplit("-", 2)[0]


def baseline_other_ready(
    statuses: Sequence[PodStatusObservation],
    target_pod: str,
    namespace: str,
    start: datetime,
    end: datetime,
) -> bool | None:
    """Whether a pod of ``target_pod``'s workload other than it may have been ``Ready`` in ``[start, end]``.

    ``None`` when the lifecycle ledger holds nothing of the target itself, so it cannot show the workload. A pod
    counts as possibly ``Ready`` when an observation shows it ``Ready`` inside the interval, or when its last
    observation before the interval shows it ``Ready`` (nothing shows it stopped).
    """
    workload = _workload(target_pod)
    mine = [s for s in statuses if s.pod.namespace == namespace and s.pod.name == target_pod]
    if not mine:
        return None
    others: dict[str, list[PodStatusObservation]] = {}
    for status in statuses:
        name = status.pod.name
        if status.pod.namespace == namespace and name != target_pod and _workload(name) == workload:
            others.setdefault(name, []).append(status)
    for observations in others.values():
        for status in observations:
            if (
                status.ready is True
                and status.observed_at >= start
                and ((status.ready_since or status.observed_at) <= end)
            ):
                return True
        before = [s for s in observations if s.observed_at < start]
        if before and max(before, key=lambda s: s.observed_at).ready is True:
            return True
    return False
