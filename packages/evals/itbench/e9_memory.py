"""Scenario-local, replayable E9 investigation memory."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from packages.evals.itbench.e9_packing import bounded_pack


@dataclass(frozen=True, slots=True)
class E9Event:
    """One bounded append-only case event."""

    event_id: str
    case_id: str
    sequence_number: int
    turn: int
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "case_id": self.case_id,
            "sequence_number": self.sequence_number,
            "turn": self.turn,
            "event_type": self.event_type,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }


class E9CaseMemory:
    """Event log plus a deterministic materialized projection."""

    def __init__(self, *, execution_id: str, scenario_id: str, case_id: str | None = None) -> None:
        self.execution_id = execution_id
        self.scenario_id = scenario_id
        self.case_id = case_id or f"{execution_id}:{scenario_id}"
        self.events: list[E9Event] = []
        self.state: dict[str, Any] = {
            "current_phase": "OBSERVE",
            "current_hypothesis": None,
            "alternative_candidates": [],
            "hypothesis_history": [],
            "discovered_entities": {},
            "candidate_state": {},
            "tested_candidates": [],
            "evidence_by_candidate": {},
            "operations_already_run": [],
            "unresolved_question": None,
            "model_steps_used": 0,
            "semantic_actions_used": 0,
            "rejection_count": 0,
            "consecutive_rejections": 0,
            "action_rejections": 0,
            "recovered_action_rejections": 0,
            "last_rejection": None,
            "evidence": {},
            "recent_evidence": [],
            "ranking_history": [],
            "submitted_targets": [],
            "active_shortlist": [],
            "observe_actions_used": 0,
        }
        self.append("CASE_STARTED", 0, {"execution_id": execution_id, "scenario_id": scenario_id})

    def append(self, event_type: str, turn: int, payload: dict[str, Any] | None = None) -> E9Event:
        event = E9Event(
            event_id=str(uuid4()),
            case_id=self.case_id,
            sequence_number=len(self.events) + 1,
            turn=turn,
            event_type=event_type,
            payload=_bounded_payload(payload or {}),
            timestamp=datetime.now(UTC).isoformat(),
        )
        self.events.append(event)
        self._project(event)
        return event

    def discover_entities(
        self, entities: tuple[dict[str, Any], ...], *, turn: int = 0
    ) -> dict[str, str]:
        """Assign immutable C### handles to observable canonical identities."""
        mapping = self.state["discovered_entities"]
        for item in entities:
            canonical = item.get("canonical")
            if not isinstance(canonical, str):
                namespace = item.get("namespace", "_cluster")
                kind = item.get("kind")
                name = item.get("name")
                if not all(isinstance(value, str) for value in (namespace, kind, name)):
                    continue
                canonical = f"{namespace}/{kind}/{name}"
            if canonical not in mapping:
                handle = f"C{len(mapping) + 1:03d}"
                self.append(
                    "ENTITY_DISCOVERED",
                    turn,
                    {
                        "entity_handle": handle,
                        "canonical": canonical,
                        "metadata": _compact(item),
                        "discovered_turn": turn,
                    },
                )
        return {value["handle"]: canonical for canonical, value in mapping.items()}

    def set_candidate_status(
        self,
        *,
        turn: int,
        handle: str,
        status: str,
        supporting_refs: tuple[str, ...] = (),
        contradicting_refs: tuple[str, ...] = (),
        rationale: str = "",
        reconsider: bool = False,
    ) -> None:
        """Apply one bounded, auditable candidate transition."""
        if self.resolve(handle) is None:
            raise ValueError(f"unknown candidate handle: {handle}")
        if status not in {"ACTIVE", "SUPPORTED", "CONTRADICTED", "REJECTED"}:
            raise ValueError(f"unsupported candidate status: {status}")
        if any(
            ref not in self.state["evidence"] for ref in (*supporting_refs, *contradicting_refs)
        ):
            raise ValueError("candidate status references unknown evidence")
        if status == "SUPPORTED" and not supporting_refs:
            raise ValueError("SUPPORTED requires supporting evidence")
        if status == "CONTRADICTED" and not contradicting_refs:
            raise ValueError("CONTRADICTED requires contradicting evidence")
        self.append(
            "CANDIDATE_STATUS_CHANGED",
            turn,
            {
                "entity_handle": handle,
                "status": status,
                "supporting_refs": list(supporting_refs),
                "contradicting_refs": list(contradicting_refs),
                "rationale": rationale[:300],
            },
        )

    def resolve(self, handle: str) -> str | None:
        for item in self.state["discovered_entities"].values():
            if item["handle"] == handle:
                return str(item["canonical"])
        return None

    def handle_for(self, canonical: str) -> str | None:
        item = self.state["discovered_entities"].get(canonical)
        return str(item["handle"]) if isinstance(item, dict) else None

    def add_evidence(
        self, *, turn: int, handle: str | None, operation: str, category: str, summary: Any
    ) -> str:
        evidence_handle = f"E{len(self.state['evidence']) + 1:03d}"
        self.append(
            "EVIDENCE_CREATED",
            turn,
            {
                "evidence_handle": evidence_handle,
                "entity_handle": handle,
                "operation": operation,
                "category": category,
                "compact_summary": _compact(summary),
                "result_status": summary.get("result_status")
                if isinstance(summary, dict)
                else None,
                "usable": summary.get("usable") if isinstance(summary, dict) else None,
            },
        )
        return evidence_handle

    def evidence_for(self, handle: str) -> tuple[str, ...]:
        values = []
        for evidence_handle, item in self.state["evidence"].items():
            if item.get("entity_handle") == handle:
                values.append(evidence_handle)
        return tuple(values)

    def has_operation(self, handle: str | None, operation: str) -> bool:
        return any(
            item.get("entity_handle") == handle and item.get("operation") == operation
            for item in self.state["operations_already_run"]
        )

    def recheck_allowed(self, handle: str | None, operation: str) -> bool:
        """Allow one further check only after a negative/inconclusive result."""
        if handle is None:
            return False
        prior = [
            item
            for item in self.state.get("evidence", {}).values()
            if item.get("entity_handle") == handle and item.get("operation") == operation
        ]
        return len(prior) == 1 and str(prior[-1].get("result_status")) in {
            "NO_DATA",
            "NEGATIVE_FINDING",
            "INCONCLUSIVE",
        }

    def set_active_shortlist(self, handles: tuple[str, ...], *, turn: int) -> None:
        """Persist the runtime-visible shortlist shared with E11 memory."""
        if any(self.resolve(handle) is None for handle in handles):
            raise ValueError("shortlist contains an unknown candidate handle")
        self.append("SHORTLIST_UPDATED", turn, {"handles": list(handles)[:20]})

    def projection(self) -> dict[str, Any]:
        """Return only bounded operational state suitable for a model context."""
        return {
            "current_phase": self.state["current_phase"],
            "current_hypothesis": self.state["current_hypothesis"],
            "alternative_candidates": self.state["alternative_candidates"][-8:],
            "hypothesis_history": self.state["hypothesis_history"][-6:],
            "discovered_entities": list(self.state["discovered_entities"].values())[:20],
            "candidate_state": self.state["candidate_state"],
            "active_candidates": [self.state["current_hypothesis"].get("entity_handle")]
            if isinstance(self.state.get("current_hypothesis"), dict)
            else [],
            "tested_candidates": self.state["tested_candidates"][-12:],
            "evidence_by_candidate": self.state["evidence_by_candidate"],
            "operations_already_run": self.state["operations_already_run"][-20:],
            "unresolved_question": self.state["unresolved_question"],
            "model_steps_used": self.state["model_steps_used"],
            "semantic_actions_used": self.state["semantic_actions_used"],
            "action_rejections": self.state["action_rejections"],
            "recovered_action_rejections": self.state["recovered_action_rejections"],
            "consecutive_rejections": self.state["consecutive_rejections"],
            "last_rejection": self.state["last_rejection"],
            "evidence": list(self.state["evidence"].values())[-12:],
            "recent_evidence": list(self.state.get("recent_evidence", []))[-8:],
            "ranking_history": self.state["ranking_history"][-8:],
            "submitted_targets": self.state["submitted_targets"],
            "active_shortlist": self.state["active_shortlist"],
        }

    def persist(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "execution_id": self.execution_id,
            "scenario_id": self.scenario_id,
            "case_id": self.case_id,
            "events": [item.as_dict() for item in self.events],
            "state": self.projection(),
        }
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def replay(cls, payload: dict[str, Any]) -> E9CaseMemory:
        memory = cls(
            execution_id=str(payload["execution_id"]),
            scenario_id=str(payload["scenario_id"]),
            case_id=str(payload["case_id"]),
        )
        memory.events = []
        memory.state = {
            "current_phase": "OBSERVE",
            "current_hypothesis": None,
            "alternative_candidates": [],
            "hypothesis_history": [],
            "discovered_entities": {},
            "candidate_state": {},
            "tested_candidates": [],
            "evidence_by_candidate": {},
            "operations_already_run": [],
            "unresolved_question": None,
            "model_steps_used": 0,
            "semantic_actions_used": 0,
            "rejection_count": 0,
            "consecutive_rejections": 0,
            "action_rejections": 0,
            "recovered_action_rejections": 0,
            "last_rejection": None,
            "evidence": {},
            "recent_evidence": [],
            "ranking_history": [],
            "submitted_targets": [],
            "active_shortlist": [],
            "observe_actions_used": 0,
        }
        for raw in payload.get("events", []):
            event = E9Event(
                event_id=str(raw["event_id"]),
                case_id=str(raw["case_id"]),
                sequence_number=int(raw["sequence_number"]),
                turn=int(raw["turn"]),
                event_type=str(raw["event_type"]),
                payload=dict(raw.get("payload", {})),
                timestamp=str(raw.get("timestamp", "")),
            )
            memory.events.append(event)
            memory._project(event)
        return memory

    def _project(self, event: E9Event) -> None:
        payload = event.payload
        if event.event_type == "ENTITY_DISCOVERED":
            handle = payload.get("entity_handle")
            canonical = payload.get("canonical")
            if isinstance(handle, str) and isinstance(canonical, str):
                self.state["discovered_entities"][canonical] = {
                    "handle": handle,
                    "canonical": canonical,
                    "metadata": payload.get("metadata", {}),
                    "discovered_turn": int(payload.get("discovered_turn", event.turn)),
                }
            return
        if event.event_type == "CANDIDATE_STATUS_CHANGED":
            handle = payload.get("entity_handle")
            if isinstance(handle, str):
                self.state["candidate_state"][handle] = {
                    "entity_handle": handle,
                    "status": payload.get("status"),
                    "supporting_refs": list(payload.get("supporting_refs", [])),
                    "contradicting_refs": list(payload.get("contradicting_refs", [])),
                    "short_rationale": payload.get("rationale", ""),
                }
            return
        if event.event_type == "ACTION_ACCEPTED":
            if self.state["consecutive_rejections"]:
                self.state["recovered_action_rejections"] += 1
            self.state["consecutive_rejections"] = 0
            self.state["last_rejection"] = None
            return
        if event.event_type == "HYPOTHESIS_PROPOSED":
            hypothesis = {
                "entity_handle": payload.get("entity_handle"),
                "rationale": payload.get("rationale", ""),
            }
            previous = self.state.get("current_hypothesis")
            if isinstance(previous, dict) and previous.get("entity_handle") != hypothesis.get(
                "entity_handle"
            ):
                self.state["alternative_candidates"].append(previous)
            self.state["current_hypothesis"] = hypothesis
            self.state["hypothesis_history"].append({**hypothesis, "event_id": event.event_id})
            self.state["current_phase"] = "VERIFY"
            self.state["unresolved_question"] = _question_for(hypothesis)
        elif event.event_type == "HYPOTHESIS_REVISED":
            previous = self.state.get("current_hypothesis")
            if isinstance(previous, dict):
                self.state["alternative_candidates"].append(previous)
            self.state["current_hypothesis"] = payload.get("hypothesis")
            self.state["hypothesis_history"].append(
                {"revised": payload.get("hypothesis"), "event_id": event.event_id}
            )
            self.state["current_phase"] = "VERIFY"
            revised_hypothesis: Any = payload.get("hypothesis")
            if isinstance(revised_hypothesis, dict):
                self.state["unresolved_question"] = _question_for(revised_hypothesis)
        elif event.event_type == "EVIDENCE_CREATED":
            handle = payload.get("evidence_handle")
            if isinstance(handle, str):
                self.state["evidence"][handle] = payload
                self.state.setdefault("recent_evidence", []).append(payload)
                self.state["recent_evidence"] = self.state["recent_evidence"][-8:]
                candidate = payload.get("entity_handle")
                if isinstance(candidate, str):
                    self.state["evidence_by_candidate"].setdefault(candidate, []).append(handle)
                    if candidate not in self.state["tested_candidates"]:
                        self.state["tested_candidates"].append(candidate)
            self.state["semantic_actions_used"] += 1
        elif event.event_type == "OBSERVATION_COMPLETED":
            if payload.get("entity_handle") is None:
                self.state["observe_actions_used"] += 1
        elif event.event_type == "EVIDENCE_ASSESSMENT":
            evidence_handle = payload.get("evidence_handle")
            if isinstance(evidence_handle, str) and evidence_handle in self.state["evidence"]:
                self.state["evidence"][evidence_handle]["assessment"] = payload.get("assessment")
                self.state["evidence"][evidence_handle]["dimension"] = payload.get("dimension")
        elif event.event_type == "RANKING_REVISION":
            self.state["ranking_history"].append(
                {
                    "revision": payload.get("revision"),
                    "triggering_evidence_ref": payload.get("triggering_evidence_ref"),
                    "handles": list(payload.get("handles", []))[:20],
                }
            )
        elif event.event_type == "SHORTLIST_UPDATED":
            self.state["active_shortlist"] = list(payload.get("handles", []))[:20]
        elif event.event_type == "ACTION_REJECTED":
            self.state["action_rejections"] += 1
            self.state["rejection_count"] += 1
            self.state["consecutive_rejections"] += 1
            self.state["last_rejection"] = {
                "turn": event.turn,
                "attempted_action": payload.get("attempted_action"),
                "attempted_target": payload.get("attempted_target"),
                "attempted_operation": payload.get("attempted_operation"),
                "code": payload.get("code", "OTHER"),
                "reason": payload.get("reason", "safe action rejected"),
                "valid_actions": list(payload.get("valid_actions", []))[:8],
                "valid_operations": list(payload.get("valid_operations", []))[:12],
            }
        elif event.event_type == "MODEL_STEP":
            self.state["model_steps_used"] += 1
        elif event.event_type in {"DIAGNOSIS_SUBMITTED", "CASE_STOPPED"}:
            self.state["current_phase"] = "CONCLUDE"
            if event.event_type == "DIAGNOSIS_SUBMITTED":
                self.state["submitted_targets"] = list(payload.get("targets", []))
        elif event.event_type == "OPERATION_REQUESTED":
            item = {
                "entity_handle": payload.get("entity_handle"),
                "operation": payload.get("operation"),
            }
            if item not in self.state["operations_already_run"]:
                self.state["operations_already_run"].append(item)
            if self.state.get("current_hypothesis"):
                self.state["unresolved_question"] = (
                    f"Does {self.state['current_hypothesis'].get('entity_handle')} "
                    f"explain the incident under {payload.get('operation')}?"
                )[:300]
        elif event.event_type == "QUESTION_SET":
            question = payload.get("question")
            self.state["unresolved_question"] = (
                question[:300] if isinstance(question, str) else None
            )


def _compact(value: Any, limit: int = 900) -> Any:
    return bounded_pack(value, limit)


def _bounded_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _compact(item, 600) for key, item in value.items()}


def _question_for(hypothesis: dict[str, Any]) -> str:
    return f"Which observable evidence verifies {hypothesis.get('entity_handle')} as causal?"[:300]


__all__ = ["E9CaseMemory", "E9Event"]
