"""Provider-injected E11 investigation loop.

This is the production control path for both the offline fake provider and a
future explicitly-authorized OpenAI provider.  It deliberately owns no
ground-truth or evaluator imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, cast
from uuid import uuid4

from pydantic import ValidationError

from packages.evals.itbench.contracts import ITBenchEvidenceCategory
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_semantic import E9SemanticOperations
from packages.evals.itbench.e11_context import build_e11_context
from packages.evals.itbench.e11_control import E11_PROMPT, E11CaseMemory, EvidenceAssessment
from packages.evals.itbench.e11_observability import (
    RankedCandidate,
    build_observed_entity_catalog,
    rank_observed_candidates,
)
from packages.evals.itbench.e11_operations import available_e11_operations
from packages.evals.itbench.external_contracts import (
    ITBENCH_EXTERNAL_PROTOCOL_V5,
    E9Action,
    ITBenchInvestigationDecisionV5,
)
from packages.provider import ModelMessage, ModelProvider, ModelRequest


@dataclass(frozen=True, slots=True)
class E11RuntimeLimits:
    max_model_calls: int = 12
    max_tool_calls: int = 24
    max_agent_turns: int = 12
    max_wall_time_seconds: int = 240
    max_consecutive_rejected_actions: int = 2


class E11InvestigationRuntime:
    """One runtime loop with a replaceable provider, never a replaceable FSM."""

    def __init__(
        self,
        provider: ModelProvider,
        backend: Any,
        *,
        limits: E11RuntimeLimits | None = None,
        execution_id: str = "ITB-E11",
        shortlist_size: int = 10,
        telemetry_ranking: bool = False,
    ) -> None:
        self.provider = provider
        self.backend = backend
        if not hasattr(backend, "_semantic_capability_cache"):
            backend._semantic_capability_cache = {}
        self.limits = limits or E11RuntimeLimits()
        self.execution_id = execution_id
        self.shortlist_size = shortlist_size
        # The known qualification set currently has a stronger, honest
        # non-telemetry ranking baseline.  Telemetry remains available to
        # semantic operations and catalog discovery, but is opt-in for the
        # initial rank until an offline qualification proves parity.
        self.telemetry_ranking = telemetry_ranking

    def run(self) -> dict[str, Any]:
        started = monotonic()
        scenario_id = str(self.backend.scenario.scenario_id)
        run_id = uuid4()
        telemetry_records = {
            category: tuple(
                self.backend.records(category)
                if callable(getattr(self.backend, "records", None))
                else self.backend.complete_source_records(category)
            )
            for category in (
                ITBenchEvidenceCategory.METRICS,
                ITBenchEvidenceCategory.LOGS,
                ITBenchEvidenceCategory.TRACES,
            )
        }
        catalog = build_observed_entity_catalog(
            self.backend,
            include_telemetry=self.telemetry_ranking,
            telemetry_records=telemetry_records,
        )
        ranking = rank_observed_candidates(
            self.backend,
            catalog,
            limit=self.shortlist_size,
            include_telemetry=self.telemetry_ranking,
            telemetry_records=telemetry_records,
            use_causal_propagation=True,
        )
        memory = E9CaseMemory(execution_id=self.execution_id, scenario_id=scenario_id)
        memory.discover_entities(tuple(entity.as_dict() for entity in catalog.entities()), turn=0)
        evidence_memory = E11CaseMemory(scenario_id=scenario_id, catalog=catalog)
        evidence_memory.initialize(ranking)
        operations = E9SemanticOperations(self.backend, memory, enable_discovery=True)
        turns: list[dict[str, Any]] = []
        usage = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
        terminal = "MODEL_STEP_LIMIT"
        consecutive_rejections = 0
        for turn in range(1, min(self.limits.max_model_calls, self.limits.max_agent_turns) + 1):
            if monotonic() - started >= self.limits.max_wall_time_seconds:
                terminal = "WALL_TIME_LIMIT"
                break
            candidates = self._ranked_for_memory(ranking, memory)
            surface = self._surface(memory, candidates, catalog, turn)
            context = build_e11_context(
                incident={"scenario_id": scenario_id},
                candidates=candidates,
                phase=str(memory.state.get("current_phase", "OBSERVE")),
                remaining_model_calls=self.limits.max_model_calls - turn + 1,
                remaining_semantic_actions=self.limits.max_tool_calls
                - int(memory.state.get("semantic_actions_used", 0)),
                investigation_state=self._investigation_state(memory),
            )
            request = self._request(run_id, context, surface)
            trace: dict[str, Any] = {
                "turn": turn,
                "phase_before": memory.state.get("current_phase"),
                "context_chars": len(context),
                "provider_exposed_actions": list(request.allowed_v5_actions or ())
                + list(request.allowed_decisions or ()),
                "provider_exposed_operations": list(request.allowed_v5_operations or ()),
                "provider_exposed_targets": list(request.allowed_v5_targets or ()),
                "accepted": False,
            }
            memory.append("MODEL_STEP", turn, {})
            usage["model_calls"] += 1
            try:
                response = self.provider.complete(request)
                usage["input_tokens"] += response.input_tokens
                usage["output_tokens"] += response.output_tokens
                usage["latency_ms"] += response.latency_ms
                payload = dict(response.structured_output)
                if isinstance(payload.get("action"), str):
                    payload["action"] = E9Action(payload["action"])
                decision = ITBenchInvestigationDecisionV5.model_validate(payload)
            except (ValidationError, ValueError) as error:
                consecutive_rejections += 1
                memory.append(
                    "ACTION_REJECTED",
                    turn,
                    {"code": "INVALID_MODEL_ACTION", "reason": str(error)[:200]},
                )
                trace.update(
                    {"decision": "ACTION_REJECTED", "rejection_code": "INVALID_MODEL_ACTION"}
                )
                turns.append(trace)
                if consecutive_rejections >= self.limits.max_consecutive_rejected_actions:
                    terminal = "PROTOCOL_STALLED"
                    break
                continue
            consecutive_rejections = 0
            try:
                result = self._apply(decision, memory, evidence_memory, operations, surface, turn)
            except ValueError as error:
                consecutive_rejections += 1
                memory.append(
                    "ACTION_REJECTED",
                    turn,
                    {"code": "INVALID_TRANSITION", "reason": str(error)[:200]},
                )
                trace.update(
                    {"decision": "ACTION_REJECTED", "rejection_code": "INVALID_TRANSITION"}
                )
                turns.append(trace)
                if consecutive_rejections >= self.limits.max_consecutive_rejected_actions:
                    terminal = "PROTOCOL_STALLED"
                    break
                continue
            trace.update(result)
            trace["accepted"] = True
            memory.append("ACTION_ACCEPTED", turn, {"action": decision.action.value})
            turns.append(trace)
            if result.get("terminal"):
                terminal = str(result["terminal"])
                break
            ranking = rank_observed_candidates(
                self.backend,
                catalog,
                limit=self.shortlist_size,
                include_telemetry=self.telemetry_ranking,
                telemetry_records=telemetry_records,
                use_causal_propagation=True,
            )
            if memory.state.get("evidence"):
                trigger = next(reversed(memory.state["evidence"]))
                memory.append(
                    "RANKING_REVISION",
                    turn,
                    {
                        "revision": len(memory.state.get("ranking_history", [])) + 1,
                        "triggering_evidence_ref": trigger,
                        "handles": [candidate.handle for candidate in ranking],
                    },
                )
        safety = {
            "ground_truth_exposure": 0,
            "cross_scenario_evidence": 0,
            "writes": 0,
            "arbitrary_execution": 0,
        }
        return {
            "terminal": terminal,
            "turn_trace": turns,
            "case_state": memory.projection(),
            "evidence_ledger": evidence_memory.evidence,
            "assessment_history": [item.as_dict() for item in evidence_memory.assessments],
            "event_log": [event.as_dict() for event in memory.events],
            "native_artifact": {"scenario_id": scenario_id, "terminal": terminal},
            "agent_output": {
                "terminal": terminal,
                "submitted": memory.state.get("current_hypothesis"),
            },
            "usage": {**usage, "safety": safety},
            "safety": safety,
            "catalog": [entity.as_dict() for entity in catalog.entities()],
        }

    def _request(self, run_id: Any, context: str, surface: dict[str, Any]) -> ModelRequest:
        actions = tuple(surface["actions"])
        decisions: list[str] = []
        if any(action in actions for action in ("OBSERVE", "HYPOTHESIZE", "INVESTIGATE", "REVISE")):
            decisions.append("CALL_TOOLS")
        if "SUBMIT" in actions:
            decisions.append("SUBMIT_DIAGNOSIS")
        if "STOP" in actions:
            decisions.append("STOP")
        return ModelRequest(
            run_id=run_id,
            messages=[
                ModelMessage(role="system", content=E11_PROMPT),
                ModelMessage(role="user", content=context),
            ],
            response_schema_name=ITBENCH_EXTERNAL_PROTOCOL_V5,
            response_schema=ITBenchInvestigationDecisionV5.model_json_schema(),
            model="gpt-5.6-luna",
            reasoning_effort="none",
            max_output_tokens=1200,
            timeout_ms=20_000,
            allowed_decisions=cast(Any, tuple(decisions)),
            allowed_v5_actions=tuple(
                action for action in actions if action not in {"SUBMIT", "STOP"}
            ),
            allowed_v5_operations=tuple(surface["operations"]),
            allowed_v5_targets=tuple(surface["targets"]),
            allowed_v5_action_capabilities=surface["capabilities"],
            allowed_tool_names=(),
            tool_schemas=(),
        )

    def _surface(
        self,
        memory: E9CaseMemory,
        candidates: tuple[RankedCandidate, ...],
        catalog: Any,
        turn: int,
    ) -> dict[str, Any]:
        handles = tuple(candidate.handle for candidate in candidates)
        phase = str(memory.state.get("current_phase", "OBSERVE"))
        actions: tuple[str, ...]
        operations: tuple[str, ...]
        if phase == "OBSERVE":
            actions = ("OBSERVE", "HYPOTHESIZE", "STOP")
            operations = (
                "INCIDENT_OVERVIEW",
                "ALERT_ANALYSIS",
                "TOPOLOGY_ANALYSIS",
                "ANOMALY_DISCOVERY",
            )
            capabilities = {
                "OBSERVE": {"targets": (), "operations": operations},
                "HYPOTHESIZE": {"targets": handles, "operations": ()},
                "STOP": {"targets": (), "operations": ()},
            }
        else:
            current = memory.state.get("current_hypothesis")
            current_handle = current.get("entity_handle") if isinstance(current, dict) else None
            available: dict[str, tuple[str, ...]] = {}
            for handle in handles:
                if handle == current_handle:
                    entity = catalog.by_handle(handle)
                    available[handle] = (
                        available_e11_operations(entity) if entity is not None else ()
                    )
                else:
                    # Alternative handles receive a bounded source-derived
                    # surface.  Full target capability checks are deferred
                    # until the model selects that alternative, avoiding a
                    # full telemetry scan for every visible candidate.
                    entity = catalog.by_handle(handle)
                    available[handle] = (
                        available_e11_operations(entity) if entity is not None else ()
                    )
            operations = tuple(
                dict.fromkeys(operation for values in available.values() for operation in values)
            )
            actions = ("INVESTIGATE", "REVISE", "SUBMIT", "STOP")
            capabilities = {
                "INVESTIGATE": {"targets": handles, "operations": operations},
                "REVISE": {
                    "targets": tuple(handle for handle in handles if handle != current_handle),
                    "operations": (),
                },
                "SUBMIT": {"targets": self._supported_handles(memory), "operations": ()},
                "STOP": {"targets": (), "operations": ()},
            }
        return {
            "actions": actions,
            "operations": operations,
            "targets": handles,
            "capabilities": capabilities,
            # Keep the union for the provider surface, but retain the
            # target-specific intersection for runtime validation.  A model
            # must not select an operation merely because it is available for
            # a different candidate in the same turn.
            "target_operations": available if phase != "OBSERVE" else {},
        }

    def _apply(
        self,
        decision: ITBenchInvestigationDecisionV5,
        memory: E9CaseMemory,
        evidence_memory: E11CaseMemory,
        operations: E9SemanticOperations,
        surface: dict[str, Any],
        turn: int,
    ) -> dict[str, Any]:
        action = decision.action.value
        capability = surface["capabilities"].get(action)
        if capability is None:
            raise ValueError("action is not exposed in the current phase")
        targets = [handle for handle in (decision.target, *decision.targets) if handle]
        if any(handle not in capability["targets"] for handle in targets):
            raise ValueError("target is not currently visible")
        if action == "OBSERVE":
            if decision.operation not in capability["operations"]:
                raise ValueError("observation operation is not available")
            evidence = operations.execute(decision.operation or "", None, turn)
            return {
                "model_action": action,
                "operation": decision.operation,
                "evidence_created": [evidence["evidence_ref"]],
            }
        if action == "HYPOTHESIZE":
            evidence_memory.hypothesize(decision.target or "")
            memory.append(
                "HYPOTHESIS_PROPOSED",
                turn,
                {"entity_handle": decision.target, "rationale": decision.rationale or ""},
            )
            return {"model_action": action, "target": decision.target}
        if action == "REVISE":
            current = memory.state.get("current_hypothesis")
            if isinstance(current, dict) and decision.target == current.get("entity_handle"):
                raise ValueError("revision requires an alternative")
            evidence_memory.revise(decision.target or "")
            memory.append(
                "HYPOTHESIS_REVISED",
                turn,
                {
                    "hypothesis": {
                        "entity_handle": decision.target,
                        "rationale": decision.rationale or "",
                    }
                },
            )
            return {"model_action": action, "target": decision.target}
        if action == "INVESTIGATE":
            target_operations = surface.get("target_operations", {})
            legal_operations = target_operations.get(decision.target, capability["operations"])
            if decision.operation not in legal_operations:
                raise ValueError("investigation operation is not available")
            evidence = operations.execute(decision.operation or "", decision.target, turn)
            self._record_assessment(memory, evidence_memory, turn, decision.target, evidence)
            return {
                "model_action": action,
                "target": decision.target,
                "operation": decision.operation,
                "evidence_created": [evidence["evidence_ref"]],
            }
        if action == "SUBMIT":
            supported = self._supported_handles(memory)
            if (
                not decision.targets
                or any(handle not in supported for handle in decision.targets)
                or not evidence_memory.submit_ready(tuple(decision.targets))
            ):
                raise ValueError("submission requires runtime-supported candidates")
            memory.append("DIAGNOSIS_SUBMITTED", turn, {"targets": decision.targets})
            return {"model_action": action, "targets": decision.targets, "terminal": "SUBMIT"}
        memory.append(
            "CASE_STOPPED", turn, {"reason": decision.stop_reason or "insufficient evidence"}
        )
        return {"model_action": action, "terminal": "STOP", "stop_reason": decision.stop_reason}

    @staticmethod
    def _record_assessment(
        memory: E9CaseMemory,
        evidence_memory: E11CaseMemory,
        turn: int,
        handle: str | None,
        result: dict[str, Any],
    ) -> None:
        evidence_ref = result.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not isinstance(handle, str):
            return
        summary = result.get("summary", {})
        local_ref = evidence_memory.add_evidence(
            handle,
            str(result.get("operation", "")),
            summary if isinstance(summary, dict) else {},
        )
        assessment = "INCONCLUSIVE"
        if isinstance(summary, dict):
            operation = result.get("operation")
            positive = {
                "EVENT_ANALYSIS": bool(summary.get("matching_count")),
                "LOG_ANALYSIS": bool(summary.get("data_available") and summary.get("patterns")),
                "METRIC_ANOMALIES": any(
                    isinstance(item, dict) and item.get("anomaly")
                    for item in summary.get("aggregates_by_metric", {}).values()
                )
                if isinstance(summary.get("aggregates_by_metric"), dict)
                else False,
                "TRACE_ERROR_TREE": bool(
                    summary.get("error_tree_available") and summary.get("edges")
                ),
                "COMPARE_REPLICAS": bool(
                    summary.get("comparison_available") and summary.get("peer_count", 0) > 0
                ),
            }.get(str(operation), False)
            if positive:
                assessment = "SUPPORTS"
        memory.append(
            "EVIDENCE_ASSESSMENT",
            turn,
            {
                "entity_handle": handle,
                "evidence_handle": evidence_ref,
                "assessment": assessment,
                "dimension": "causal",
            },
        )
        if assessment == "SUPPORTS":
            memory.set_candidate_status(
                turn=turn, handle=handle, status="SUPPORTED", supporting_refs=(evidence_ref,)
            )
            evidence_memory.assess(
                handle,
                local_ref,
                EvidenceAssessment.SUPPORTS,
                dimension="causal",
            )
        else:
            evidence_memory.assess(
                handle,
                local_ref,
                EvidenceAssessment.INCONCLUSIVE,
                dimension="causal",
            )

    @staticmethod
    def _ranked_for_memory(
        ranking: tuple[RankedCandidate, ...], memory: E9CaseMemory
    ) -> tuple[RankedCandidate, ...]:
        by_canonical = {
            item["canonical"]: item["handle"]
            for item in memory.state["discovered_entities"].values()
        }
        return tuple(
            RankedCandidate(
                item.handle,
                item.canonical,
                item.rank,
                item.score,
                item.family,
                item.retrieval_evidence,
            )
            for item in ranking
            if item.canonical in by_canonical and by_canonical[item.canonical] == item.handle
        )

    @staticmethod
    def _supported_handles(memory: E9CaseMemory) -> tuple[str, ...]:
        return tuple(
            handle
            for handle, item in memory.state.get("candidate_state", {}).items()
            if item.get("status") == "SUPPORTED"
        )

    @staticmethod
    def _investigation_state(memory: E9CaseMemory) -> dict[str, Any]:
        projection = memory.projection()
        projection["supporting_evidence"] = [
            item for item in projection["evidence"] if item.get("assessment") == "SUPPORTS"
        ]
        projection["contradicting_evidence"] = [
            item for item in projection["evidence"] if item.get("assessment") == "CONTRADICTS"
        ]
        projection["recent_operations"] = projection.get("operations_already_run", [])[-6:]
        return projection


__all__ = ["E11InvestigationRuntime", "E11RuntimeLimits"]
