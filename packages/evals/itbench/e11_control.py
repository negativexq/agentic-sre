"""Offline-qualified E11 control primitives.

The historical E9 control surface remains unchanged.  E11 uses this small
event-friendly facade to make observation, evidence polarity, discovery, and
submission readiness explicit before a future provider-backed runner is
authorized.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Literal

from packages.evals.itbench.e11_observability import (
    ObservedEntity,
    ObservedEntityCatalog,
    RankedCandidate,
)
from packages.evals.itbench.e11_operations import available_e11_operations, comparable_peer_count

E11_PROMPT_VERSION = "itbench_e11_observe_verify_v1"
E11_PROMPT = """You are a read-only SRE investigator. Observe bounded incident evidence before choosing a hypothesis. Treat candidates as hypotheses, distinguish causes from downstream symptoms, prefer incident-specific and temporally aligned evidence, use discriminating verification, revise when evidence contradicts a hypothesis, submit only a causally supported candidate, and STOP when evidence is insufficient. The runtime owns identity, handles, provenance, budgets, and safety."""
E11_OBSERVATION_OPERATIONS = (
    "INCIDENT_OVERVIEW",
    "ALERT_ANALYSIS",
    "TOPOLOGY_OVERVIEW",
    "ANOMALY_DISCOVERY",
)


class EvidenceAssessment:
    SUPPORTS: Literal["SUPPORTS"] = "SUPPORTS"
    CONTRADICTS: Literal["CONTRADICTS"] = "CONTRADICTS"
    INCONCLUSIVE: Literal["INCONCLUSIVE"] = "INCONCLUSIVE"


class CandidateStatus:
    UNTESTED = "UNTESTED"
    ACTIVE = "ACTIVE"
    WEAKLY_SUPPORTED = "WEAKLY_SUPPORTED"
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class EvidenceAssessmentRecord:
    evidence_ref: str
    handle: str
    assessment: str
    dimension: str
    rationale: str

    def as_dict(self) -> dict[str, str]:
        return {
            "evidence_ref": self.evidence_ref,
            "entity_handle": self.handle,
            "assessment": self.assessment,
            "dimension": self.dimension,
            "rationale": self.rationale[:300],
        }


@dataclass(slots=True)
class E11CaseMemory:
    """Scenario-local control state with stable handles and evidence polarity."""

    scenario_id: str
    catalog: ObservedEntityCatalog
    active_shortlist: tuple[RankedCandidate, ...] = ()
    phase: str = "OBSERVE"
    current_hypothesis: str | None = None
    candidate_status: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    assessments: list[EvidenceAssessmentRecord] = field(default_factory=list)
    ranking_history: list[dict[str, Any]] = field(default_factory=list)
    semantic_actions_used: int = 0

    def initialize(self, shortlist: tuple[RankedCandidate, ...]) -> None:
        self.active_shortlist = shortlist
        self.candidate_status.update(
            {candidate.handle: CandidateStatus.UNTESTED for candidate in shortlist}
        )
        self.ranking_history.append(
            {
                "revision": 0,
                "triggering_evidence_ref": None,
                "handles": [candidate.handle for candidate in shortlist],
            }
        )

    def observe(self, shortlist: tuple[RankedCandidate, ...]) -> dict[str, Any]:
        """Expose compact bounded global observation before hypothesis selection."""
        self.active_shortlist = shortlist
        self.candidate_status.update(
            {
                candidate.handle: self.candidate_status.get(
                    candidate.handle, CandidateStatus.UNTESTED
                )
                for candidate in shortlist
            }
        )
        return {
            "phase": self.phase,
            "candidate_count": len(shortlist),
            "candidates": [candidate.as_dict() for candidate in shortlist],
            "observation_operations": E11_OBSERVATION_OPERATIONS,
        }

    def hypothesize(self, handle: str) -> None:
        self._require_visible(handle)
        self.current_hypothesis = handle
        self.candidate_status[handle] = CandidateStatus.ACTIVE
        self.phase = "VERIFY"

    def revise(self, handle: str) -> None:
        self._require_visible(handle)
        if handle == self.current_hypothesis:
            raise ValueError("REVISE requires an alternative candidate")
        self.current_hypothesis = handle
        self.candidate_status[handle] = CandidateStatus.ACTIVE
        self.phase = "VERIFY"

    def add_evidence(self, handle: str, operation: str, summary: dict[str, Any]) -> str:
        self._require_catalog_handle(handle)
        evidence_ref = f"E{len(self.evidence) + 1:03d}"
        self.evidence[evidence_ref] = {
            "evidence_ref": evidence_ref,
            "entity_handle": handle,
            "operation": operation,
            "summary": _bounded_summary(summary),
        }
        self.semantic_actions_used += 1
        return evidence_ref

    def assess(
        self,
        handle: str,
        evidence_ref: str,
        assessment: Literal["SUPPORTS", "CONTRADICTS", "INCONCLUSIVE"],
        *,
        dimension: str,
        rationale: str = "",
    ) -> EvidenceAssessmentRecord:
        self._require_catalog_handle(handle)
        if evidence_ref not in self.evidence:
            raise ValueError("assessment references unknown evidence")
        if self.evidence[evidence_ref].get("entity_handle") != handle:
            raise ValueError("assessment evidence does not belong to candidate")
        if assessment not in {
            EvidenceAssessment.SUPPORTS,
            EvidenceAssessment.CONTRADICTS,
            EvidenceAssessment.INCONCLUSIVE,
        }:
            raise ValueError("unsupported evidence assessment")
        record = EvidenceAssessmentRecord(
            evidence_ref=evidence_ref,
            handle=handle,
            assessment=assessment,
            dimension=dimension[:64],
            rationale=rationale,
        )
        self.assessments.append(record)
        self._refresh_status(handle)
        return record

    def rerank(self, shortlist: tuple[RankedCandidate, ...], triggering_evidence_ref: str) -> None:
        """Record a ranking revision without renaming any runtime handle."""
        if triggering_evidence_ref not in self.evidence:
            raise ValueError("ranking trigger references unknown evidence")
        old = [candidate.handle for candidate in self.active_shortlist]
        self.active_shortlist = shortlist
        self.candidate_status.update(
            {
                candidate.handle: self.candidate_status.get(
                    candidate.handle, CandidateStatus.UNTESTED
                )
                for candidate in shortlist
            }
        )
        self.ranking_history.append(
            {
                "revision": len(self.ranking_history),
                "triggering_evidence_ref": triggering_evidence_ref,
                "previous_handles": old,
                "handles": [candidate.handle for candidate in shortlist],
            }
        )

    def submit_ready(self, handles: tuple[str, ...]) -> bool:
        """Require supported causal evidence and no unresolved contradiction."""
        if not handles or self.current_hypothesis not in handles:
            return False
        return all(
            self.candidate_status.get(handle) == CandidateStatus.SUPPORTED for handle in handles
        )

    def _refresh_status(self, handle: str) -> None:
        candidate = [item for item in self.assessments if item.handle == handle]
        if any(item.assessment == EvidenceAssessment.CONTRADICTS for item in candidate):
            self.candidate_status[handle] = CandidateStatus.CONTRADICTED
        elif any(item.assessment == EvidenceAssessment.SUPPORTS for item in candidate):
            self.candidate_status[handle] = CandidateStatus.SUPPORTED
        elif candidate:
            self.candidate_status[handle] = CandidateStatus.WEAKLY_SUPPORTED

    def _require_visible(self, handle: str) -> None:
        if handle not in {candidate.handle for candidate in self.active_shortlist}:
            raise ValueError("candidate handle is not on the active shortlist")
        self._require_catalog_handle(handle)

    def _require_catalog_handle(self, handle: str) -> ObservedEntity:
        entity = self.catalog.by_handle(handle)
        if entity is None:
            raise ValueError("unknown runtime candidate handle")
        return entity


@dataclass(frozen=True, slots=True)
class E11ControlSurface:
    phase: str
    actions: tuple[str, ...]
    operations: tuple[str, ...]
    targets: tuple[str, ...]
    current_hypothesis: str | None


def e11_control_surface(memory: E11CaseMemory, *, final_turn: bool = False) -> E11ControlSurface:
    """Derive model-visible actions from E11 state, including OBSERVE first."""
    targets = tuple(candidate.handle for candidate in memory.active_shortlist)
    if final_turn:
        actions: tuple[str, ...] = (
            ("SUBMIT", "STOP")
            if memory.submit_ready((memory.current_hypothesis or "",))
            else ("STOP",)
        )
        return E11ControlSurface(memory.phase, actions, (), targets, memory.current_hypothesis)
    if memory.phase == "OBSERVE":
        return E11ControlSurface(
            memory.phase,
            ("OBSERVE", "HYPOTHESIZE", "STOP"),
            E11_OBSERVATION_OPERATIONS,
            targets,
            None,
        )
    if memory.phase == "VERIFY":
        operations = _verification_operations(memory)
        actions = (
            ("INVESTIGATE", "REVISE", "SUBMIT", "STOP")
            if operations
            else ("REVISE", "SUBMIT", "STOP")
        )
        return E11ControlSurface(
            memory.phase, actions, operations, targets, memory.current_hypothesis
        )
    return E11ControlSurface(memory.phase, ("STOP",), (), targets, memory.current_hypothesis)


def _verification_operations(memory: E11CaseMemory) -> tuple[str, ...]:
    if memory.current_hypothesis is None:
        return ()
    entity = memory.catalog.by_handle(memory.current_hypothesis)
    if entity is None:
        return ()
    return available_e11_operations(
        entity, comparable_peers=comparable_peer_count(memory.catalog, entity)
    )


def _bounded_summary(summary: dict[str, Any]) -> dict[str, Any]:
    encoded = str(summary)
    return {"summary": encoded[:2_000], "summary_hash": sha256(encoded.encode()).hexdigest()}


def e11_prompt_hash() -> str:
    return sha256(E11_PROMPT.encode()).hexdigest()


__all__ = [
    "CandidateStatus",
    "E11CaseMemory",
    "E11ControlSurface",
    "E11_PROMPT_VERSION",
    "E11_OBSERVATION_OPERATIONS",
    "EvidenceAssessment",
    "EvidenceAssessmentRecord",
    "e11_control_surface",
    "e11_prompt_hash",
]
