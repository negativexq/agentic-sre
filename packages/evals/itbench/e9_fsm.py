"""Pure phase policy compatibility surface for the E9 case reducer."""

from __future__ import annotations

from enum import StrEnum


class E9Phase(StrEnum):
    OBSERVE = "OBSERVE"
    HYPOTHESIZE = "HYPOTHESIZE"
    VERIFY = "VERIFY"
    REVISE = "REVISE"
    CONCLUDE = "CONCLUDE"


class E9FSM:
    """Stateless compatibility facade; CaseState owns the actual phase."""

    def __init__(self, *, max_consecutive_rejections: int = 2) -> None:
        self.max_consecutive_rejections = max_consecutive_rejections

    def valid_actions(
        self,
        *,
        has_hypothesis: bool,
        evidence_count: int,
        final_turn: bool,
        phase: str | None = None,
    ) -> tuple[str, ...]:
        current = phase or E9Phase.OBSERVE.value
        if final_turn:
            return ("SUBMIT", "STOP") if has_hypothesis and evidence_count else ("STOP",)
        if current == E9Phase.OBSERVE:
            return ("OBSERVE", "HYPOTHESIZE", "STOP")
        if current == E9Phase.VERIFY:
            return ("OBSERVE", "INVESTIGATE", "REVISE", "SUBMIT", "STOP")
        return ("OBSERVE", "HYPOTHESIZE", "INVESTIGATE", "REVISE", "STOP")

    @staticmethod
    def transition(phase: str, action: str) -> str:
        if action == "HYPOTHESIZE" or action == "INVESTIGATE" or action == "REVISE":
            return E9Phase.VERIFY.value
        if action in {"SUBMIT", "STOP"}:
            return E9Phase.CONCLUDE.value
        return phase


__all__ = ["E9FSM", "E9Phase"]
