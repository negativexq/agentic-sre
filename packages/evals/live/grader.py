"""Grade a live diagnosis against what a scenario staged.

Grading is deliberately strict in one direction and generous in another.  A
scenario that staged a real change is only correct when the engine names the
actor that changed.  A scenario that staged no change is only correct when the
engine names nothing at all: a confident answer against an empty change set is
a fabrication, not a near miss.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from packages.evals.live.scenarios import Abstain, LiveScenario, RootCause


class Outcome(StrEnum):
    """How one scenario run ended."""

    CORRECT = "CORRECT"
    """The engine produced the expected answer."""

    WRONG_ACTOR = "WRONG_ACTOR"
    """A root cause was named, but not the one that was staged."""

    FABRICATED = "FABRICATED"
    """A root cause was named although nothing changed."""

    MISSED = "MISSED"
    """A real change was staged but the engine named nothing."""

    NO_INCIDENT = "NO_INCIDENT"
    """The fault never became an incident; the scenario did not reach the engine."""

    NO_DIAGNOSIS = "NO_DIAGNOSIS"
    """An incident opened but no diagnosis was stored in time."""

    ERROR = "ERROR"
    """Staging or teardown failed; the run carries no signal."""


PASSING = (Outcome.CORRECT,)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """The graded outcome of one scenario run."""

    scenario_id: str
    outcome: Outcome
    expected: str
    actual: str
    resolution: str = ""
    confidence: str = ""
    finding_kinds: tuple[str, ...] = ()
    supported_by_expected_kind: bool = True
    incident_id: str = ""
    alert_seconds: float | None = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome in PASSING


def _entity_label(root_cause: Any) -> str:
    if not isinstance(root_cause, dict):
        return ""
    namespace = root_cause.get("namespace") or ""
    kind = root_cause.get("kind") or ""
    name = root_cause.get("name") or ""
    if not kind or not name:
        return ""
    return f"{namespace}/{kind}/{name}" if namespace else f"{kind}/{name}"


def _matches(expected: RootCause, root_cause: dict[str, Any]) -> bool:
    """Match on kind and name, tolerating the pod/replicaset the actor owns.

    A Deployment answer, the ReplicaSet it owns, and a Pod that ReplicaSet
    created are the same finding to an operator, so a name that starts with the
    expected name is accepted for those owned kinds.
    """
    kind = str(root_cause.get("kind") or "")
    name = str(root_cause.get("name") or "")
    if kind == expected.kind and name == expected.name:
        return True
    owned = {"Pod", "ReplicaSet"}
    if expected.kind == "Deployment" and kind in owned:
        return name.startswith(f"{expected.name}-")
    return False


def grade(scenario: LiveScenario, document: dict[str, Any] | None) -> ScenarioResult:
    """Compare one stored diagnosis against the scenario's expectation."""
    expected_label = scenario.expectation.label
    if document is None:
        return ScenarioResult(
            scenario_id=scenario.id,
            outcome=Outcome.NO_DIAGNOSIS,
            expected=expected_label,
            actual="",
            detail="no diagnosis document was stored",
        )

    root_cause = document.get("root_cause")
    resolution = str(document.get("resolution") or "")
    confidence = str(document.get("confidence") or "")
    evidence = document.get("evidence") or ()
    finding_kinds = tuple(
        str(item.get("kind"))
        for item in evidence
        if isinstance(item, dict) and item.get("kind") is not None
    )
    actual = _entity_label(root_cause)

    if isinstance(scenario.expectation, Abstain):
        outcome = Outcome.CORRECT if root_cause is None else Outcome.FABRICATED
        return ScenarioResult(
            scenario_id=scenario.id,
            outcome=outcome,
            expected=expected_label,
            actual=actual or "ABSTAIN",
            resolution=resolution,
            confidence=confidence,
            finding_kinds=finding_kinds,
            detail=scenario.expectation.because,
        )

    expectation = scenario.expectation
    if root_cause is None:
        return ScenarioResult(
            scenario_id=scenario.id,
            outcome=Outcome.MISSED,
            expected=expected_label,
            actual="ABSTAIN",
            resolution=resolution,
            confidence=confidence,
            finding_kinds=finding_kinds,
        )

    if not _matches(expectation, root_cause):
        return ScenarioResult(
            scenario_id=scenario.id,
            outcome=Outcome.WRONG_ACTOR,
            expected=expected_label,
            actual=actual,
            resolution=resolution,
            confidence=confidence,
            finding_kinds=finding_kinds,
        )

    supported = not expectation.finding_kinds or bool(
        set(expectation.finding_kinds) & set(finding_kinds)
    )
    return ScenarioResult(
        scenario_id=scenario.id,
        outcome=Outcome.CORRECT,
        expected=expected_label,
        actual=actual,
        resolution=resolution,
        confidence=confidence,
        finding_kinds=finding_kinds,
        supported_by_expected_kind=supported,
        detail=(
            ""
            if supported
            else f"correct actor, but none of {', '.join(expectation.finding_kinds)} backed it"
        ),
    )


@dataclass(frozen=True, slots=True)
class SuiteReport:
    """Aggregate outcome across a scenario run."""

    results: tuple[ScenarioResult, ...]

    @property
    def graded(self) -> tuple[ScenarioResult, ...]:
        """Results that carry signal, excluding harness failures."""
        return tuple(
            item
            for item in self.results
            if item.outcome not in {Outcome.ERROR, Outcome.NO_INCIDENT}
        )

    @property
    def correct(self) -> int:
        return sum(item.passed for item in self.results)

    @property
    def fabrications(self) -> int:
        return sum(item.outcome is Outcome.FABRICATED for item in self.results)

    def accuracy(self) -> float:
        graded = self.graded
        return self.correct / len(graded) if graded else 0.0

    def render(self) -> str:
        """Render a fixed-width table plus the headline numbers."""
        width = max((len(item.scenario_id) for item in self.results), default=10)
        lines = [
            f"{item.scenario_id.ljust(width)}  {item.outcome.value.ljust(13)} "
            f"expected={item.expected} actual={item.actual or '-'}"
            + (f" [{item.detail}]" if item.detail and not item.passed else "")
            for item in self.results
        ]
        graded = self.graded
        lines.append("")
        lines.append(
            f"correct {self.correct}/{len(graded)} graded "
            f"({self.accuracy():.1%}); {len(self.results) - len(graded)} not graded; "
            f"{self.fabrications} fabricated"
        )
        return "\n".join(lines)
