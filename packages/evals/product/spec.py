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
from typing import Literal

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


MANIFESTATION_ONLY = "MANIFESTATION_ONLY"


@dataclass(frozen=True, slots=True)
class TimelineRef:
    """One staged action: the phase at ``offset`` and the action's index within it."""

    offset: timedelta
    index: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.offset, timedelta) or self.index < 0:
            raise ValueError("TimelineRef is a phase offset and a non-negative action index")


@dataclass(frozen=True, slots=True)
class TargetHypothesisRef:
    """How the proofs find ``H_x`` among the persisted R1 hypotheses (M19-7.P1).

    ``action_receipt``: the exact Pod instance a verified staged action touched
    (its receipt's UID). ``static_actor``: an exact ``namespace/Kind/name``.
    ``mechanism`` is the persisted mechanism class, compared as an exact set:
    ``{MANIFESTATION_ONLY}`` or the initiating finding kinds. ``episode_basis``
    optionally names the expected A1 basis (``TERMINATED``/``RECOVERED``).
    The persisted ``hypothesis_key`` is the result of selection, never an input.
    """

    source: Literal["action_receipt", "static_actor"]
    mechanism: frozenset[str]
    action: TimelineRef | None = None
    actor: str | None = None
    episode_basis: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mechanism, frozenset) or not self.mechanism:
            raise ValueError("TargetHypothesisRef.mechanism is a non-empty frozenset")
        if MANIFESTATION_ONLY in self.mechanism and len(self.mechanism) > 1:
            raise ValueError("MANIFESTATION_ONLY excludes initiating kinds")
        if self.source == "action_receipt":
            if self.action is None or self.actor is not None:
                raise ValueError("an action_receipt reference names a timeline action only")
        elif self.source == "static_actor":
            if not self.actor or self.actor.count("/") != 2 or self.action is not None:
                raise ValueError("a static_actor reference names namespace/Kind/name only")
        else:
            raise ValueError(f"unknown TargetHypothesisRef source {self.source!r}")


@dataclass(frozen=True, slots=True)
class Expectation:
    """Which §13 proofs the scenario must pass and what they are about.

    A proof whose metadata is missing cannot be evaluated and FAILs; it is not
    refused here, so an expectation can be declared before its data exists.
    """

    proofs: tuple[ProofId, ...] = ()
    target_hypothesis_ref: TargetHypothesisRef | None = None
    target_rule: tuple[str, str] | None = None  # (rule_id, rule_version)
    target_consequence: str | None = None  # ROOT_INELIGIBILITY | CONTRADICTION
    root_actor: str | None = None  # namespace/Kind/name

    def __post_init__(self) -> None:
        if not isinstance(self.proofs, tuple) or not all(
            isinstance(proof, ProofId) for proof in self.proofs
        ):
            raise TypeError("Expectation.proofs is a tuple of ProofId")
        if len(set(self.proofs)) != len(self.proofs):
            raise ValueError("Expectation.proofs repeats a proof")
        if self.target_rule is not None and (
            len(self.target_rule) != 2 or not all(self.target_rule)
        ):
            raise ValueError("Expectation.target_rule is (rule_id, rule_version)")
        if self.target_consequence not in (None, "ROOT_INELIGIBILITY", "CONTRADICTION"):
            raise ValueError(
                "Expectation.target_consequence is ROOT_INELIGIBILITY or CONTRADICTION"
            )
        if self.root_actor is not None and self.root_actor.count("/") != 2:
            raise ValueError("Expectation.root_actor is namespace/Kind/name")


@dataclass(frozen=True, slots=True)
class ProductScenario:
    """One product-resolution scenario: explicit id, ordered phases, expectation."""

    scenario_id: str
    phases: tuple[Phase, ...]
    expectation: Expectation
    description: str = ""
    # F7 DEV scenarios are "DEV"; a smoke/no-op scenario has no tier.
    tier: Literal["DEV"] | None = None

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
        if self.tier not in ("DEV", None):
            raise ValueError("ProductScenario.tier is 'DEV' or None")


__all__ = [
    "MANIFESTATION_ONLY",
    "Expectation",
    "Phase",
    "ProductScenario",
    "ProofId",
    "TargetHypothesisRef",
    "TimelineRef",
]
