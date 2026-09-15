"""E9 single-agent harness: minimal actions, soft FSM, memory and planning."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from packages.contracts import Alert, Incident
from packages.evals.itbench.e9_context import E9ContextPlanner
from packages.evals.itbench.e9_fsm import E9FSM
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_semantic import E9_SEMANTIC_OPERATIONS, E9SemanticOperations
from packages.evals.itbench.external_contracts import (
    ITBENCH_EXTERNAL_PROTOCOL_V5,
    E9Action,
    ITBenchInvestigationDecisionV5,
)
from packages.provider import (
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ProviderError,
    ProviderErrorCode,
)

ITBENCH_E9_PROMPT_VERSION = "itbench_sre_investigator_v7"
ITBENCH_E9_PROMPT = """You are a read-only SRE investigator. Use the incident and the runtime-owned case file to find the smallest independently causal Kubernetes entity set.

Follow this procedure: observe symptoms; form a hypothesis; verify or refute it with one discriminating semantic operation; revise when evidence contradicts the hypothesis; submit only a minimal causal set. Use differential observability, duration matching, observable breadcrumbs, temporal alignment, and upstream irreducibility. A downstream symptom, existing ConfigMap, policy, or chaos object is not proof of causality.

The harness owns workflow state, entity identity, evidence provenance, budgets, and candidate memory. Use only runtime candidate handles such as C017. Do not construct canonical identities or evidence references. Choose one currently exposed semantic operation. Invalid safe actions are rejected with valid next actions; correct the action and continue. Submit or STOP when sufficient evidence is reached. Never write, remediate, use shell, filesystem, SQL, arbitrary PromQL, ground truth, or evaluator data."""


def e9_prompt_hash() -> str:
    return sha256(ITBENCH_E9_PROMPT.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class E9Limits:
    """E9 horizon, independent from the legacy E1--E8 limit envelope."""

    max_model_calls: int = 12
    max_tool_calls: int = 24
    max_agent_turns: int = 12
    max_wall_time_seconds: int = 240

    def __post_init__(self) -> None:
        if (
            min(
                self.max_model_calls,
                self.max_tool_calls,
                self.max_agent_turns,
                self.max_wall_time_seconds,
            )
            <= 0
        ):
            raise ValueError("E9 limits must be positive")


class E9InvestigationRuntime:
    """Runtime-owned orchestration around the minimal V5 action contract."""

    def __init__(
        self,
        provider: ModelProvider,
        backend: Any,
        *,
        limits: E9Limits | None = None,
        execution_id: str = "ITB-E9",
        max_consecutive_rejected_actions: int = 2,
    ) -> None:
        self.provider = provider
        self.backend = backend
        self.limits = limits or E9Limits()
        self.execution_id = execution_id
        self.max_consecutive_rejected_actions = max_consecutive_rejected_actions
        self.planner = E9ContextPlanner()

    def build_request(
        self,
        incident: Incident,
        alerts: tuple[Alert, ...],
        memory: E9CaseMemory,
        fsm: E9FSM,
        *,
        run_id: UUID,
        turn: int,
    ) -> tuple[ModelRequest, dict[str, int]]:
        context, sections = self.planner.plan(
            self.backend,
            incident,
            alerts,
            memory,
            fsm,
            turn=turn,
            max_steps=self.limits.max_model_calls,
            semantic_limit=self.limits.max_tool_calls,
        )
        final_turn = turn >= self.limits.max_model_calls
        allowed: tuple[Literal["CALL_TOOLS", "SUBMIT_DIAGNOSIS", "STOP"], ...] = (
            ("SUBMIT_DIAGNOSIS", "STOP")
            if final_turn
            else ("CALL_TOOLS", "SUBMIT_DIAGNOSIS", "STOP")
        )
        request = ModelRequest(
            run_id=run_id,
            messages=[
                ModelMessage(role="system", content=ITBENCH_E9_PROMPT),
                ModelMessage(role="user", content=context),
            ],
            response_schema_name=ITBENCH_EXTERNAL_PROTOCOL_V5,
            response_schema=ITBenchInvestigationDecisionV5.model_json_schema(),
            model="gpt-5.6-luna",
            reasoning_effort="none",
            max_output_tokens=1_200,
            timeout_ms=20_000,
            allowed_decisions=allowed,
            allowed_tool_names=(),
            tool_schemas=(),
        )
        return request, sections

    def run(self, incident: Incident, alerts: tuple[Alert, ...] = ()) -> dict[str, Any]:
        run_id = uuid4()
        started = monotonic()
        scenario_id = getattr(self.backend.scenario, "scenario_id", "Scenario-1")
        memory = E9CaseMemory(execution_id=self.execution_id, scenario_id=scenario_id)
        fsm = E9FSM(max_consecutive_rejections=self.max_consecutive_rejected_actions)
        memory.discover_entities(self.backend.candidate_entities(limit=10))
        operations = E9SemanticOperations(self.backend, memory)
        turns: list[dict[str, Any]] = []
        context_metrics: list[dict[str, Any]] = []
        model_calls = 0
        input_tokens = 0
        output_tokens = 0
        provider_latency = 0
        terminal = "MODEL_STEP_LIMIT"
        submitted_entities: list[str] = []
        submitted_refs: list[str] = []
        accounting_reader = getattr(self.provider, "accounting_snapshot", None)
        before = accounting_reader() if callable(accounting_reader) else None

        for turn in range(1, self.limits.max_model_calls + 1):
            if monotonic() - started >= self.limits.max_wall_time_seconds:
                terminal = "WALL_TIME_LIMIT"
                break
            model_calls += 1
            memory.append("MODEL_STEP", turn, {})
            request, sections = self.build_request(
                incident, alerts, memory, fsm, run_id=run_id, turn=turn
            )
            context_metrics.append(
                {"turn": turn, "context_chars": sections.get("total", 0), "sections": sections}
            )
            try:
                response = self.provider.complete(request)
            except ProviderError as error:
                if error.code in {
                    ProviderErrorCode.FUNCTION_ARGUMENTS_SCHEMA_INVALID,
                    ProviderErrorCode.JSON_DECODE_FAILED,
                    ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                }:
                    self._reject(
                        memory,
                        turn,
                        "provider returned a safe schema-invalid action",
                        "PROVIDER_SCHEMA",
                    )
                    if (
                        memory.state["consecutive_rejections"]
                        > self.max_consecutive_rejected_actions
                    ):
                        terminal = "PROTOCOL_STALLED"
                        break
                    continue
                terminal = "PROVIDER_ERROR"
                turns.append(
                    {"turn": turn, "decision": terminal, "provider_error": error.code.value}
                )
                break
            provider_latency += response.latency_ms
            input_tokens += int(response.input_tokens or 0)
            output_tokens += int(response.output_tokens or 0)
            try:
                decision_payload = dict(response.structured_output)
                if isinstance(decision_payload.get("action"), str):
                    decision_payload["action"] = E9Action(decision_payload["action"])
                decision = ITBenchInvestigationDecisionV5.model_validate(decision_payload)
            except ValidationError as error:
                self._reject(
                    memory,
                    turn,
                    "safe action shape was invalid",
                    str(error.errors()[0].get("type", "validation_error")),
                )
                turns.append(
                    {"turn": turn, "decision": "ACTION_REJECTED", "reason": "invalid_action_shape"}
                )
                if memory.state["consecutive_rejections"] > self.max_consecutive_rejected_actions:
                    terminal = "PROTOCOL_STALLED"
                    break
                continue
            action = decision.action.value
            has_hypothesis = memory.state.get("current_hypothesis") is not None
            final_turn = turn >= self.limits.max_model_calls
            target_handles = [item for item in (decision.target, *decision.targets) if item]
            unknown_handles = [item for item in target_handles if memory.resolve(item) is None]
            operation = decision.operation or ""
            precondition_error: str | None = None
            precondition_code = "INVALID_ACTION"
            if unknown_handles:
                precondition_error = "target is not an observable candidate handle: " + ", ".join(
                    unknown_handles
                )
                precondition_code = "UNKNOWN_CANDIDATE"
            elif action == "INVESTIGATE" and operation not in E9_SEMANTIC_OPERATIONS:
                precondition_error = "operation is not currently supported"
                precondition_code = "UNSUPPORTED_OPERATION"
            elif final_turn and action not in {"SUBMIT", "STOP"}:
                precondition_error = "final model step permits only SUBMIT or STOP"
                precondition_code = "FINAL_TURN_ACTION"
            elif action == "SUBMIT" and (not has_hypothesis or not memory.state["evidence"]):
                precondition_error = (
                    "SUBMIT requires a hypothesis and at least one evidence-producing action"
                )
                precondition_code = "SUBMIT_PRECONDITION"
            elif action == "REVISE" and decision.target is None:
                precondition_error = "REVISE requires a candidate target"
                precondition_code = "REVISE_TARGET"
            elif action in {"HYPOTHESIZE", "REVISE"} and decision.target:
                candidate = memory.state["candidate_state"].get(decision.target, {})
                if candidate.get("status") == "REJECTED":
                    precondition_error = "rejected candidates cannot be silently reactivated"
                    precondition_code = "REJECTED_CANDIDATE"
            if precondition_error is not None:
                self._reject(memory, turn, precondition_error, precondition_code)
                turns.append(
                    {
                        "turn": turn,
                        "decision": "ACTION_REJECTED",
                        "reason": precondition_error,
                        "valid_next_actions": list(
                            fsm.valid_actions(
                                has_hypothesis=has_hypothesis,
                                evidence_count=len(memory.state["evidence"]),
                                final_turn=final_turn,
                            )
                        ),
                    }
                )
                if memory.state["consecutive_rejections"] > self.max_consecutive_rejected_actions:
                    terminal = "PROTOCOL_STALLED"
                    break
                continue
            accepted, reason = fsm.accept(
                action,
                has_hypothesis=has_hypothesis,
                evidence_count=len(memory.state["evidence"]),
                final_turn=final_turn,
            )
            if not accepted:
                self._reject(memory, turn, reason, "INVALID_TRANSITION")
                turns.append(
                    {
                        "turn": turn,
                        "decision": "ACTION_REJECTED",
                        "reason": reason,
                        "valid_next_actions": list(
                            fsm.valid_actions(
                                has_hypothesis=has_hypothesis,
                                evidence_count=len(memory.state["evidence"]),
                                final_turn=final_turn,
                            )
                        ),
                    }
                )
                if memory.state["consecutive_rejections"] > self.max_consecutive_rejected_actions:
                    terminal = "PROTOCOL_STALLED"
                    break
                continue
            if memory.state["consecutive_rejections"]:
                memory.state["recovered_action_rejections"] += 1
            memory.state["consecutive_rejections"] = 0
            memory.state["current_phase"] = fsm.phase.value
            if action == "STOP":
                terminal = "STOP"
                memory.append(
                    "CASE_STOPPED", turn, {"reason": decision.stop_reason or "model_stop"}
                )
                turns.append({"turn": turn, "decision": terminal, "phase": fsm.phase.value})
                break
            if action == "SUBMIT":
                for handle in decision.targets:
                    canonical = memory.resolve(handle)
                    if canonical is None:
                        self._reject(
                            memory, turn, f"unknown candidate handle: {handle}", "UNKNOWN_CANDIDATE"
                        )
                        break
                    submitted_entities.append(canonical)
                    submitted_refs.extend(memory.evidence_for(handle))
                if len(submitted_entities) != len(decision.targets):
                    turns.append(
                        {
                            "turn": turn,
                            "decision": "ACTION_REJECTED",
                            "reason": "unknown candidate handle",
                        }
                    )
                    continue
                terminal = "SUBMIT"
                memory.append(
                    "DIAGNOSIS_SUBMITTED",
                    turn,
                    {"targets": decision.targets, "submitted_entities": submitted_entities},
                )
                turns.append(
                    {
                        "turn": turn,
                        "decision": terminal,
                        "targets": decision.targets,
                        "phase": fsm.phase.value,
                    }
                )
                break
            if action == "HYPOTHESIZE":
                memory.set_candidate_status(
                    turn=turn,
                    handle=decision.target or "",
                    status="ACTIVE",
                    rationale=decision.rationale or "",
                )
                memory.append(
                    "HYPOTHESIS_PROPOSED",
                    turn,
                    {"entity_handle": decision.target, "rationale": decision.rationale or ""},
                )
            elif action == "REVISE":
                memory.set_candidate_status(
                    turn=turn,
                    handle=decision.target or "",
                    status="ACTIVE",
                    rationale=decision.rationale or "",
                )
                hypothesis = {
                    "entity_handle": decision.target,
                    "rationale": decision.rationale or "",
                }
                memory.append("HYPOTHESIS_REVISED", turn, {"hypothesis": hypothesis})
            else:
                operation = decision.operation or (
                    "INCIDENT_OVERVIEW" if action == "OBSERVE" else ""
                )
                if operation not in E9_SEMANTIC_OPERATIONS:
                    self._reject(
                        memory,
                        turn,
                        "operation is not currently supported",
                        "UNSUPPORTED_OPERATION",
                    )
                    continue
                if memory.has_operation(decision.target, operation):
                    turns.append(
                        {
                            "turn": turn,
                            "decision": "ACTION_REJECTED",
                            "reason": "DUPLICATE_OPERATION",
                        }
                    )
                    self._reject(
                        memory,
                        turn,
                        "operation already completed for this target",
                        "DUPLICATE_OPERATION",
                    )
                    continue
                if memory.state["semantic_actions_used"] >= self.limits.max_tool_calls:
                    terminal = "SEMANTIC_ACTION_LIMIT"
                    break
                try:
                    observation = operations.execute(operation, decision.target, turn)
                except (ValueError, KeyError) as error:
                    self._reject(memory, turn, str(error), "SEMANTIC_ACTION_REJECTED")
                    continue
                turns.append(
                    {
                        "turn": turn,
                        "decision": action,
                        "operation": operation,
                        "target": decision.target,
                        "evidence_ref": observation["evidence_ref"],
                        "phase": fsm.phase.value,
                    }
                )
        else:
            terminal = "MODEL_STEP_LIMIT"

        if terminal == "MODEL_STEP_LIMIT" and not turns:
            terminal = "MODEL_STEP_LIMIT"
        if callable(accounting_reader):
            after = accounting_reader()
            provider_invocations = after.provider_invocations - (
                before.provider_invocations if before else 0
            )
            outbound_attempts = after.outbound_api_attempts - (
                before.outbound_api_attempts if before else 0
            )
        else:
            provider_invocations = model_calls
            outbound_attempts = (
                model_calls if getattr(self.provider, "provider_name", "") == "openai" else 0
            )
        return {
            "protocol": ITBENCH_EXTERNAL_PROTOCOL_V5,
            "execution_id": self.execution_id,
            "case_id": memory.case_id,
            "scenario_id": scenario_id,
            "incident_id": str(incident.incident_id),
            "terminal": terminal,
            "turns": turns,
            "events": [event.as_dict() for event in memory.events],
            "case_state": memory.projection(),
            "evidence": list(memory.state["evidence"].values()),
            "submitted_entities": list(dict.fromkeys(submitted_entities)),
            "submitted_evidence_refs": list(dict.fromkeys(submitted_refs)),
            "usage": {
                "model_steps": model_calls,
                "model_calls": model_calls,
                "model": "gpt-5.6-luna",
                "reasoning_effort": "none",
                "semantic_actions_requested": sum(
                    item.get("decision") in {"OBSERVE", "INVESTIGATE"} for item in turns
                ),
                "semantic_actions_executed": memory.state["semantic_actions_used"],
                "backend_reads": memory.state["semantic_actions_used"],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "provider_latency_ms": provider_latency,
                "context_metrics": context_metrics,
                "action_rejections": memory.state["action_rejections"],
                "recovered_action_rejections": memory.state["recovered_action_rejections"],
                "consecutive_rejection_max": self.max_consecutive_rejected_actions,
                "source_performance": self.backend.performance_snapshot(),
                "provider_invocations": provider_invocations,
                "outbound_api_attempts": outbound_attempts,
            },
            "safety": {
                "ground_truth_exposure": 0,
                "cross_scenario_evidence": 0,
                "writes": 0,
                "arbitrary_execution": 0,
            },
            "duration_ms": int((monotonic() - started) * 1000),
        }

    @staticmethod
    def _reject(memory: E9CaseMemory, turn: int, reason: str, code: str) -> None:
        memory.append("ACTION_REJECTED", turn, {"reason": reason[:300], "code": code})


__all__ = [
    "E9Limits",
    "E9InvestigationRuntime",
    "ITBENCH_E9_PROMPT",
    "ITBENCH_E9_PROMPT_VERSION",
    "e9_prompt_hash",
]
