"""Clean-baseline verdict of the product-resolution harness (M19-6.8).

The control plane's incident-free probe evaluates the persisted evidence just
before staging. A baseline that already yields an initiating finding, or a
root-eligible manifestation-only hypothesis, would compete with the staged
root, so the run is ``ERROR``: it is never retried or cleaned up.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class DirtyBaseline(RuntimeError):
    """The persisted baseline already contains a root candidate."""

    def __init__(self, initiating: tuple[str, ...], manifestation_only: tuple[str, ...]) -> None:
        self.initiating = initiating
        self.manifestation_only = manifestation_only
        super().__init__(
            "dirty baseline: "
            f"{len(initiating)} initiating finding(s) {list(initiating)}, "
            f"{len(manifestation_only)} root-eligible manifestation-only hypothesis(es) "
            f"{list(manifestation_only)}"
        )


def _ids(document: Mapping[str, Any], ids_key: str, count_key: str) -> tuple[str, ...]:
    ids = document.get(ids_key)
    count = document.get(count_key)
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise ValueError(f"baseline evaluation lacks {ids_key}")
    if not isinstance(count, int) or isinstance(count, bool) or count != len(ids):
        raise ValueError(f"baseline evaluation {count_key} does not match {ids_key}")
    return tuple(ids)


def check_baseline(document: Mapping[str, Any]) -> None:
    """Return when the baseline is clean; raise ``DirtyBaseline`` otherwise."""
    initiating = _ids(document, "initiating_finding_ids", "initiating_finding_count")
    manifestation_only = _ids(
        document,
        "root_eligible_manifestation_only_hypothesis_ids",
        "root_eligible_manifestation_only_count",
    )
    if initiating or manifestation_only:
        raise DirtyBaseline(initiating, manifestation_only)


__all__ = ["DirtyBaseline", "check_baseline"]
