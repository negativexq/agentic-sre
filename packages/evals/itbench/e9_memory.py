"""Scenario-local, replayable E9 investigation memory."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


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
            "evidence": {},
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

    def discover_entities(self, entities: tuple[dict[str, Any], ...]) -> dict[str, str]:
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
                    0,
                    {"entity_handle": handle, "canonical": canonical, "metadata": _compact(item)},
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
    ) -> None:
        """Apply one bounded, auditable candidate transition."""
        if self.resolve(handle) is None:
            raise ValueError(f"unknown candidate handle: {handle}")
        if status not in {"ACTIVE", "SUPPORTED", "REJECTED"}:
            raise ValueError(f"unsupported candidate status: {status}")
        if any(
            ref not in self.state["evidence"] for ref in (*supporting_refs, *contradicting_refs)
        ):
            raise ValueError("candidate status references unknown evidence")
        previous = self.state["candidate_state"].get(handle, {}).get("status")
        if previous == "REJECTED" and status != "REJECTED":
            raise ValueError("rejected candidates cannot be silently reactivated")
        if status == "SUPPORTED" and not supporting_refs:
            raise ValueError("SUPPORTED requires supporting evidence")
        active_count = sum(
            item.get("status") == "ACTIVE" for item in self.state["candidate_state"].values()
        )
        if status == "ACTIVE" and previous != "ACTIVE" and active_count >= 3:
            raise ValueError("maximum active candidates exceeded")
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

    def projection(self) -> dict[str, Any]:
        """Return only bounded operational state suitable for a model context."""
        return {
            "current_phase": self.state["current_phase"],
            "current_hypothesis": self.state["current_hypothesis"],
            "hypothesis_history": self.state["hypothesis_history"][-6:],
            "discovered_entities": list(self.state["discovered_entities"].values())[:20],
            "candidate_state": self.state["candidate_state"],
            "active_candidates": [
                handle
                for handle, item in self.state["candidate_state"].items()
                if item.get("status") == "ACTIVE"
            ],
            "tested_candidates": self.state["tested_candidates"][-12:],
            "evidence_by_candidate": self.state["evidence_by_candidate"],
            "operations_already_run": self.state["operations_already_run"][-20:],
            "unresolved_question": self.state["unresolved_question"],
            "model_steps_used": self.state["model_steps_used"],
            "semantic_actions_used": self.state["semantic_actions_used"],
            "action_rejections": self.state["action_rejections"],
            "consecutive_rejections": self.state["consecutive_rejections"],
            "evidence": list(self.state["evidence"].values())[-12:],
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
            "evidence": {},
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
        if event.event_type == "HYPOTHESIS_PROPOSED":
            hypothesis = {
                "entity_handle": payload.get("entity_handle"),
                "rationale": payload.get("rationale", ""),
            }
            self.state["current_hypothesis"] = hypothesis
            self.state["hypothesis_history"].append({**hypothesis, "event_id": event.event_id})
            self.state["current_phase"] = "VERIFY"
        elif event.event_type == "HYPOTHESIS_REVISED":
            self.state["current_hypothesis"] = payload.get("hypothesis")
            self.state["hypothesis_history"].append(
                {"revised": payload.get("hypothesis"), "event_id": event.event_id}
            )
            self.state["current_phase"] = "REVISE"
        elif event.event_type == "EVIDENCE_CREATED":
            handle = payload.get("evidence_handle")
            if isinstance(handle, str):
                self.state["evidence"][handle] = payload
                candidate = payload.get("entity_handle")
                if isinstance(candidate, str):
                    self.state["evidence_by_candidate"].setdefault(candidate, []).append(handle)
                    if candidate not in self.state["tested_candidates"]:
                        self.state["tested_candidates"].append(candidate)
            self.state["semantic_actions_used"] += 1
        elif event.event_type == "ACTION_REJECTED":
            self.state["action_rejections"] += 1
            self.state["rejection_count"] += 1
            self.state["consecutive_rejections"] += 1
        elif event.event_type in {
            "OBSERVATION_COMPLETED",
            "HYPOTHESIS_PROPOSED",
            "HYPOTHESIS_REVISED",
            "EVIDENCE_CREATED",
        }:
            self.state["consecutive_rejections"] = 0
        elif event.event_type == "MODEL_STEP":
            self.state["model_steps_used"] += 1
        elif event.event_type == "OPERATION_REQUESTED":
            item = {
                "entity_handle": payload.get("entity_handle"),
                "operation": payload.get("operation"),
            }
            if item not in self.state["operations_already_run"]:
                self.state["operations_already_run"].append(item)


def _compact(value: Any, limit: int = 900) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded) <= limit:
        return value
    return {"summary": encoded[: max(1, limit - 40)], "truncated": True}


def _bounded_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _compact(item, 600) for key, item in value.items()}


__all__ = ["E9CaseMemory", "E9Event"]
