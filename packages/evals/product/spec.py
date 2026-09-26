"""Declarative product-resolution scenarios (M19-6.5).

A scenario is an ordered list of phases, each a set of actions at a fixed
offset from ``T0`` (the staged root action), plus the §13 proofs it must pass.
Negative offsets are pre-history phases before the root. Definitions carry no
runtime state and no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from packages.evals.product.actions import ProductAction


class ProofId(StrEnum):
    """The §13 predicates a live scenario can be required to pass."""

    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    T4 = "T4"
    N0 = "N0"
    N1 = "N1"
    N2 = "N2"
    N3 = "N3"


@dataclass(frozen=True, slots=True)
class Phase:
    """Actions staged at ``offset`` from ``T0``, in the given order."""

    offset: timedelta
    actions: tuple[ProductAction, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.offset, timedelta):
            raise TypeError("Phase.offset is a timedelta relative to T0")
        if not isinstance(self.actions, tuple) or not all(
            isinstance(action, ProductAction) for action in self.actions
        ):
            raise TypeError("Phase.actions is a tuple of ProductAction")


@dataclass(frozen=True, slots=True)
class Expectation:
    """Which §13 proofs the scenario must pass; the proofs themselves live in F7."""

    proofs: tuple[ProofId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.proofs, tuple) or not all(
            isinstance(proof, ProofId) for proof in self.proofs
        ):
            raise TypeError("Expectation.proofs is a tuple of ProofId")
        if len(set(self.proofs)) != len(self.proofs):
            raise ValueError("Expectation.proofs repeats a proof")


@dataclass(frozen=True, slots=True)
class ProductScenario:
    """One product-resolution scenario: explicit id, ordered phases, expectation."""

    scenario_id: str
    phases: tuple[Phase, ...]
    expectation: Expectation
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id.strip():
            raise ValueError("ProductScenario.scenario_id must be a non-empty string")
        if not isinstance(self.phases, tuple) or not all(
            isinstance(phase, Phase) for phase in self.phases
        ):
            raise TypeError("ProductScenario.phases is a tuple of Phase")
        offsets = [phase.offset for phase in self.phases]
        if any(later <= earlier for earlier, later in zip(offsets, offsets[1:], strict=False)):
            raise ValueError("phases must have strictly increasing offsets")
        if not isinstance(self.expectation, Expectation):
            raise TypeError("ProductScenario.expectation is an Expectation")


__all__ = ["Expectation", "Phase", "ProductScenario", "ProofId"]
