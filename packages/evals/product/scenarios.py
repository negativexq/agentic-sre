"""The product scenario registry (M19-6.5).

An explicit, ordered tuple: no discovery. It is empty until the DEV scenarios
of F7 are defined.
"""

from __future__ import annotations

from collections.abc import Sequence

from packages.evals.product.spec import ProductScenario


def validate_registry(items: Sequence[ProductScenario]) -> tuple[ProductScenario, ...]:
    """The registry as a tuple; a repeated scenario id fails loudly."""
    seen: set[str] = set()
    for item in items:
        if item.scenario_id in seen:
            raise ValueError(f"duplicate product scenario id: {item.scenario_id}")
        seen.add(item.scenario_id)
    return tuple(items)


SCENARIOS: tuple[ProductScenario, ...] = validate_registry(())


def scenarios() -> tuple[ProductScenario, ...]:
    """Registered scenarios in registry order."""
    return SCENARIOS


__all__ = ["SCENARIOS", "scenarios", "validate_registry"]
