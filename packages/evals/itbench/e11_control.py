"""Offline-qualified E11 control primitives.

The historical E9 control surface remains unchanged.  E11 uses this small
event-friendly facade to make observation, evidence polarity, discovery, and
submission readiness explicit before a future provider-backed runner is
authorized.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Literal

from packages.evals.itbench.e11_observability import (
    ObservedEntity,
    ObservedEntityCatalog,
    RankedCandidate,
)
from packages.evals.itbench.e11_operations import available_e11_operations, comparable_peer_count

E11_PROMPT_VERSION = "itbench_e11_observe_verify_v3"
E11_PROMPT = """You are a read-only SRE investigator. Observe bounded incident evidence before choosing a hypothesis. Treat candidates as hypotheses, distinguish causes from downstream symptoms, prefer incident-specific and temporally aligned evidence, use discriminating verification, revise when evidence contradicts a hypothesis, submit only a causally supported candidate, and STOP when evidence is insufficient. Use only runtime-issued candidate handles. An operation is legal only for the target when it appears in legal_target_operations; the global operation list is only a transport superset. Read the finding, result_status, and polarity before deciding. The runtime owns identity, handles, provenance, budgets, and safety."""
E11_OBSERVATION_OPERATIONS = (
    "INCIDENT_OVERVIEW",
    "ALERT_ANALYSIS",
    "TOPOLOGY_OVERVIEW",
    "ANOMALY_DISCOVERY",
)
E11_EVIDENCE_DIMENSIONS = frozenset(
    {
        "causal",
        "causal_mechanism",
        "temporal",
        "failure_signature",
        "comparative",
        "resource_pressure",
        "dependency",
        "configuration",
    }
)
E11_UNUSABLE_RESULT_STATUSES = frozenset({"NO_DATA", "UNAVAILABLE", "ERROR", "INCONCLUSIVE"})


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
    supersedes_evidence_ref: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "evidence_ref": self.evidence_ref,
            "entity_handle": self.handle,
            "assessment": self.assessment,
            "dimension": self.dimension,
            "rationale": self.rationale[:300],
            "supersedes_evidence_ref": self.supersedes_evidence_ref,
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

    def add_evidence(self, handle: str | None, operation: str, summary: dict[str, Any]) -> str:
        if handle is not None:
            self._require_catalog_handle(handle)
        # E9 observation references remain E### for backward compatibility;
        # E11 assessment references use an explicit namespace.
        evidence_ref = f"ASM-E{len(self.evidence) + 1:03d}"
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
        supersedes_evidence_ref: str | None = None,
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
        if dimension not in E11_EVIDENCE_DIMENSIONS:
            raise ValueError("unsupported evidence dimension")
        evidence_summary = self.evidence[evidence_ref].get("summary", {})
        if assessment == EvidenceAssessment.SUPPORTS and not _evidence_usable(evidence_summary):
            raise ValueError("unusable evidence cannot support a candidate")
        if supersedes_evidence_ref is not None:
            superseded = self.evidence.get(supersedes_evidence_ref)
            if superseded is None or superseded.get("entity_handle") != handle:
                raise ValueError("superseded evidence must belong to this candidate")
            prior_assessment = next(
                (
                    item
                    for item in reversed(self.assessments)
                    if item.evidence_ref == supersedes_evidence_ref
                ),
                None,
            )
            if prior_assessment is None or prior_assessment.dimension != dimension:
                raise ValueError("supersession must address an assessed evidence dimension")
        record = EvidenceAssessmentRecord(
            evidence_ref=evidence_ref,
            handle=handle,
            assessment=assessment,
            dimension=dimension[:64],
            rationale=rationale,
            supersedes_evidence_ref=supersedes_evidence_ref,
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

    def _supported_handles_for_submission(self) -> tuple[str, ...]:
        return tuple(
            handle
            for handle, status in self.candidate_status.items()
            if status == CandidateStatus.SUPPORTED
        )

    def _refresh_status(self, handle: str) -> None:
        candidate = [item for item in self.assessments if item.handle == handle]
        active_contradictions = {
            item.evidence_ref
            for item in candidate
            if item.assessment == EvidenceAssessment.CONTRADICTS
        }
        for item in candidate:
            if item.supersedes_evidence_ref in active_contradictions:
                active_contradictions.discard(item.supersedes_evidence_ref)
        if active_contradictions:
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
    operation_targets: tuple[tuple[str, tuple[str, ...]], ...] = ()


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
        operation_targets = tuple(
            (
                handle,
                _operations_for_handle(memory, handle),
            )
            for handle in targets
            if _operations_for_handle(memory, handle)
        )
        actions_list = ("INVESTIGATE", "REVISE", "STOP") if operations else ("REVISE", "STOP")
        if memory.submit_ready(memory._supported_handles_for_submission()):
            actions_list = (*actions_list[:-1], "SUBMIT", "STOP")
        actions = actions_list
        return E11ControlSurface(
            memory.phase,
            actions,
            operations,
            targets,
            memory.current_hypothesis,
            operation_targets,
        )
    return E11ControlSurface(memory.phase, ("STOP",), (), targets, memory.current_hypothesis)


def _verification_operations(memory: E11CaseMemory) -> tuple[str, ...]:
    operations: list[str] = []
    for candidate in memory.active_shortlist:
        operations.extend(_operations_for_handle(memory, candidate.handle))
    return tuple(dict.fromkeys(operations))


def _operations_for_handle(memory: E11CaseMemory, handle: str) -> tuple[str, ...]:
    entity = memory.catalog.by_handle(handle)
    if entity is None:
        return ()
    return available_e11_operations(
        entity, comparable_peers=comparable_peer_count(memory.catalog, entity)
    )


def _bounded_summary(summary: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str)
    status = str(summary.get("result_status", "POSITIVE_FINDING"))
    usable = summary.get("usable")
    if not isinstance(usable, bool):
        usable = status not in E11_UNUSABLE_RESULT_STATUSES
    important = {
        key: summary[key]
        for key in (
            "error_count",
            "patterns",
            "metric",
            "metric_name",
            "direction",
            "peer_count",
            "event_category",
            "reason",
            "count",
            "data_available",
            "result_status",
            "usable",
            "finding",
            "causal_findings",
            "configuration_dependency_match",
            "selector_target_match",
            "chaos_target_match",
            "mechanism_compatible",
            "timing_compatible",
            "error_origin",
            "error_origins",
            "candidate_service",
            "candidate_is_error_origin",
            "upstream",
            "downstream",
            "differences",
        )
        if key in summary
    }
    return {
        "summary": encoded[:2_000],
        "structured_fields": _bounded_fields(important),
        "summary_hash": sha256(encoded.encode()).hexdigest(),
        "result_status": status,
        "usable": usable,
    }


def _bounded_fields(value: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): item if isinstance(item, (str, int, float, bool)) else str(item)[:600]
        for key, item in value.items()
    }


def _evidence_usable(summary: Any) -> bool:
    return (
        isinstance(summary, dict)
        and summary.get("usable", True) is not False
        and str(summary.get("result_status", "POSITIVE_FINDING"))
        not in E11_UNUSABLE_RESULT_STATUSES
    )


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
