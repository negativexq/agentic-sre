"""Plain state and policy contracts used by the LangGraph wiring."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypedDict

from packages.rca.engine import Case, EngineConfig
from packages.rca.model import (
    Diagnosis,
    EntityRef,
    Finding,
    Hypothesis,
    InformationGap,
    InvestigationAction,
    InvestigationActionAudit,
    InvestigationActionStatus,
    InvestigationLedgerEntry,
    InvestigationObservation,
    InvestigationResult,
    InvestigationStep,
    InvestigationStopReason,
    Resolution,
    StructuralAlternative,
)
from packages.rca.source import ObservationSource


@dataclass(frozen=True)
class InvestigationConfig:
    """Operational limits for one investigation run."""

    max_turns: int = 6
    max_model_calls: int = 6
    max_tool_calls: int = 8
    max_tool_calls_per_gap: int = 2
    max_invalid_actions: int = 2
    max_no_progress_rounds: int = 2
    max_wall_time_seconds: float = 120.0
    engine: EngineConfig | None = None

    def __post_init__(self) -> None:
        if (
            min(
                self.max_turns,
                self.max_model_calls,
                self.max_tool_calls,
                self.max_tool_calls_per_gap,
            )
            < 1
        ):
            raise ValueError("investigation budgets must be positive")
        if self.max_invalid_actions < 0 or self.max_no_progress_rounds < 0:
            raise ValueError("investigation retry budgets cannot be negative")
        if self.max_wall_time_seconds <= 0:
            raise ValueError("max_wall_time_seconds must be positive")


@dataclass(frozen=True)
class InvestigationPolicyContext:
    """Small bounded context presented to an action policy."""

    incident_id: str
    diagnosis: Diagnosis
    hypotheses: tuple[Hypothesis, ...]
    gaps: tuple[InformationGap, ...]
    attempted_actions: tuple[str, ...]
    turns: int
    model_calls_remaining: int
    tool_calls_remaining: int
    previous_investigations: tuple[InvestigationLedgerEntry, ...] = ()
    last_rejection: tuple[str, str, str] | None = None
    structural_alternatives: tuple[StructuralAlternative, ...] = ()
    candidate_actions: tuple[InvestigationAction, ...] = ()


class InvestigationPolicy(Protocol):
    """Policy that may request one already-authorized observation."""

    counts_as_model: bool

    def choose_action(self, context: InvestigationPolicyContext) -> InvestigationAction: ...


class InvestigationTool(Protocol):
    """Read-only semantic capability used by the graph."""

    name: str

    def execute(
        self,
        case: Case,
        gap: InformationGap,
        target: EntityRef,
    ) -> InvestigationObservation: ...


CaseRebuilder = Callable[[ObservationSource, tuple[Finding, ...]], Case]


class InvestigationState(TypedDict, total=False):
    """Checkpointable bounded graph state: data only.

    Every value must serialize without pickle. Live dependencies (the
    observation source, the policy and its model client, tools, the case
    rebuilder, configuration) are bound to the graph's nodes when it is built;
    the current case is rebuilt from the bounded source plus acquired evidence
    references and legacy ``investigation_findings``.
    """

    incident_id: str
    started_at: datetime
    initial_diagnosis: Diagnosis
    current_diagnosis: Diagnosis
    observations: tuple[InvestigationObservation, ...]
    ledger: tuple[InvestigationLedgerEntry, ...]
    action_audits: tuple[InvestigationActionAudit, ...]
    investigation_findings: tuple[Finding, ...]
    acquired_evidence_refs: tuple[str, ...]
    attempted_actions: tuple[str, ...]
    attempted_observations: tuple[str, ...]
    successful_exploration_observations: tuple[str, ...]
    exploration_covered_atoms: tuple[tuple[str, str], ...]
    last_exploration_progress: bool
    intent_history: tuple[dict[str, object], ...]
    pending_intent_id: str | None
    pending_intent_kind: str | None
    attempted_gap_ids: tuple[str, ...]
    pending_action: InvestigationAction | None
    pending_observation: InvestigationObservation | None
    pending_findings: tuple[Finding, ...]
    pending_returned_evidence_refs: tuple[str, ...]
    pending_new_evidence_refs: tuple[str, ...]
    pending_already_known_refs: tuple[str, ...]
    previous_resolution: Resolution
    previous_gap_fingerprint: tuple[tuple[str, ...], ...]
    previous_evidence_fingerprint: tuple[str, ...]
    previous_hypothesis_fingerprint: tuple[str, ...]
    previous_world_model_fingerprint: str
    frontier_queried_dimensions: tuple[tuple[str, tuple[str, ...]], ...]
    action_validation_status: InvestigationActionStatus | None
    turns: int
    model_calls: int
    tool_calls: int
    invalid_actions: int
    rejected_actions: int
    no_progress_count: int
    last_new_evidence_count: int
    last_new_raw_evidence_count: int
    stop_reason: InvestigationStopReason | None
    trace_steps: tuple[InvestigationStep, ...]
    final_result: InvestigationResult | None
    last_rejection: tuple[str, str, str] | None


__all__ = [
    "CaseRebuilder",
    "InvestigationConfig",
    "InvestigationPolicy",
    "InvestigationPolicyContext",
    "InvestigationState",
    "InvestigationTool",
]
