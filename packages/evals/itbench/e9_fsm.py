"""Soft, harness-owned E9 investigation state machine."""

from __future__ import annotations

from enum import StrEnum


class E9Phase(StrEnum):
    OBSERVE = "OBSERVE"
    HYPOTHESIZE = "HYPOTHESIZE"
    VERIFY = "VERIFY"
    REVISE = "REVISE"
    CONCLUDE = "CONCLUDE"


class E9FSM:
    """Allow useful observation loops while rejecting unsafe workflow steps."""

    def __init__(self, *, max_consecutive_rejections: int = 2) -> None:
        self.phase = E9Phase.OBSERVE
        self.max_consecutive_rejections = max_consecutive_rejections
        self.consecutive_rejections = 0

    def valid_actions(
        self, *, has_hypothesis: bool, evidence_count: int, final_turn: bool
    ) -> tuple[str, ...]:
        if final_turn:
            return ("SUBMIT", "STOP") if has_hypothesis and evidence_count else ("STOP",)
        if self.phase is E9Phase.OBSERVE:
            return ("OBSERVE", "HYPOTHESIZE", "STOP")
        if self.phase is E9Phase.HYPOTHESIZE:
            return ("OBSERVE", "INVESTIGATE", "REVISE", "STOP")
        if self.phase is E9Phase.VERIFY:
            return ("OBSERVE", "INVESTIGATE", "REVISE", "SUBMIT", "STOP")
        if self.phase is E9Phase.REVISE:
            return ("OBSERVE", "HYPOTHESIZE", "INVESTIGATE", "SUBMIT", "STOP")
        return ("SUBMIT", "REVISE", "STOP")

    def accept(
        self,
        action: str,
        *,
        has_hypothesis: bool,
        evidence_count: int,
        final_turn: bool = False,
    ) -> tuple[bool, str]:
        allowed = self.valid_actions(
            has_hypothesis=has_hypothesis, evidence_count=evidence_count, final_turn=final_turn
        )
        if action not in allowed:
            self.consecutive_rejections += 1
            return (
                False,
                f"action {action} is not valid in phase {self.phase}; valid actions: {', '.join(allowed)}",
            )
        self.consecutive_rejections = 0
        if action == "OBSERVE":
            self.phase = E9Phase.OBSERVE
        elif action == "HYPOTHESIZE":
            self.phase = E9Phase.HYPOTHESIZE
        elif action == "INVESTIGATE":
            self.phase = E9Phase.VERIFY
        elif action == "REVISE":
            self.phase = E9Phase.REVISE
        elif action in {"SUBMIT", "STOP"}:
            self.phase = E9Phase.CONCLUDE
        return True, "accepted"

    def stalled(self) -> bool:
        return self.consecutive_rejections > self.max_consecutive_rejections


__all__ = ["E9FSM", "E9Phase"]
