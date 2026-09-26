"""Pure timeline helpers for product scenarios (M19-6.5).

No clock, sleep, subprocess, Kubernetes or HTTP: executing a timeline belongs
to the runner (M19-6.7).
"""

from __future__ import annotations

from datetime import timedelta

from packages.evals.product.actions import ProductAction
from packages.evals.product.spec import ProductScenario


def action_sequence(scenario: ProductScenario) -> tuple[tuple[timedelta, ProductAction], ...]:
    """Every action with its offset from T0, in phase then action order."""
    return tuple((phase.offset, action) for phase in scenario.phases for action in phase.actions)


__all__ = ["action_sequence"]
