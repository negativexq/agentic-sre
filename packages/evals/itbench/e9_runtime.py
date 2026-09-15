"""E9 single-agent runtime with one event-derived control state."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from packages.contracts import Alert, Incident
from packages.evals.itbench.e9_context import E9ContextPlanner
from packages.evals.itbench.e9_control import control_surface
from packages.evals.itbench.e9_memory import E9CaseMemory
from packages.evals.itbench.e9_semantic import E9SemanticOperations
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
ITBENCH_E9_PROMPT = """You are a read-only SRE investigator. Find the smallest independently causal Kubernetes entity set.

Observe the incident, form a hypothesis, verify or refute it with one discriminating semantic operation, revise when evidence changes direction, then submit the minimal causal set or stop. Use differential observability, duration matching, observable breadcrumbs, temporal alignment, and upstream irreducibility. A downstream symptom, existing ConfigMap, policy, or chaos object is not proof of causality.

The harness owns phase, candidate handles, identity, evidence provenance, memory, budgets, and safety. Choose only an action and operation currently exposed by the runtime. If an action is rejected, read the typed correction in the next context and choose a different valid action; do not repeat the rejected operation. Use C### handles, never construct canonical identities or evidence references. Never write, remediate, use shell, filesystem, SQL, arbitrary PromQL, ground truth, or evaluator data."""


def e9_prompt_hash() -> str:
    return sha256(ITBENCH_E9_PROMPT.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class E9Limits:
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
    """Runtime-owned loop; policy and state are derived from CaseMemory."""

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
        fsm: Any = None,
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
        surface = control_surface(
            memory.state,
            turn=turn,
            max_steps=self.limits.max_model_calls,
            max_rejections=self.max_consecutive_rejected_actions,
            semantic_limit=self.limits.max_tool_calls,
        )
        allowed_decisions: list[Any] = []
        if any(
            action in surface.actions
            for action in ("OBSERVE", "HYPOTHESIZE", "INVESTIGATE", "REVISE")
        ):
            allowed_decisions.append("CALL_TOOLS")
        if "SUBMIT" in surface.actions:
            allowed_decisions.append("SUBMIT_DIAGNOSIS")
        if "STOP" in surface.actions:
            allowed_decisions.append("STOP")
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
            max_output_tokens=1200,
            timeout_ms=20_000,
            allowed_decisions=tuple(allowed_decisions),
            allowed_v5_actions=tuple(
                action for action in surface.actions if action not in {"SUBMIT", "STOP"}
            ),
            allowed_v5_operations=surface.operations,
            allowed_tool_names=(),
            tool_schemas=(),
        )
        return request, sections

    def run(self, incident: Incident, alerts: tuple[Alert, ...] = ()) -> dict[str, Any]:
        run_id = uuid4()
        started = monotonic()
        scenario_id = getattr(self.backend.scenario, "scenario_id", "Scenario-1")
        memory = E9CaseMemory(execution_id=self.execution_id, scenario_id=scenario_id)
        memory.discover_entities(self.backend.candidate_entities(limit=10))
        operations = E9SemanticOperations(self.backend, memory, incident)
        turns: list[dict[str, Any]] = []
        context_metrics: list[dict[str, Any]] = []
        model_calls = input_tokens = output_tokens = provider_latency = 0
        terminal = "MODEL_STEP_LIMIT"
        submitted_entities: list[str] = []
        submitted_refs: list[str] = []
        accounting_reader = getattr(self.provider, "accounting_snapshot", None)
        before = accounting_reader() if callable(accounting_reader) else None

        for turn in range(1, min(self.limits.max_model_calls, self.limits.max_agent_turns) + 1):
            if monotonic() - started >= self.limits.max_wall_time_seconds:
                terminal = "WALL_TIME_LIMIT"
                break
            memory.append("MODEL_STEP", turn, {})
            model_calls += 1
            request, sections = self.build_request(
                incident, alerts, memory, None, run_id=run_id, turn=turn
            )
            context_metrics.append(
                {"turn": turn, "context_chars": sections.get("total", 0), "sections": sections}
            )
            trace: dict[str, Any] = {
                "turn": turn,
                "phase_before": memory.state["current_phase"],
                "accepted": False,
                "evidence_created": [],
                "context_chars": sections.get("total", 0),
            }
            try:
                response = self.provider.complete(request)
            except ProviderError as error:
                if error.code in {
                    ProviderErrorCode.FUNCTION_ARGUMENTS_SCHEMA_INVALID,
                    ProviderErrorCode.JSON_DECODE_FAILED,
                    ProviderErrorCode.SCHEMA_VALIDATION_FAILED,
                }:
                    surface = self._surface(memory, turn)
                    self._reject(
                        memory,
                        turn,
                        "provider returned a safe schema-invalid action",
                        "PROVIDER_SCHEMA",
                        valid_actions=surface.actions,
                        valid_operations=surface.operations,
                    )
                    trace.update(
                        {
                            "decision": "ACTION_REJECTED",
                            "rejection_code": "PROVIDER_SCHEMA",
                            "rejection_reason": "provider returned a safe schema-invalid action",
                        }
                    )
                    turns.append(trace)
                    if self._stalled(memory):
                        terminal = "PROTOCOL_STALLED"
                        break
                    continue
                terminal = "PROVIDER_ERROR"
                trace.update({"decision": terminal, "provider_error": error.code.value})
                turns.append(trace)
                break
            provider_latency += response.latency_ms
            input_tokens += int(response.input_tokens or 0)
            output_tokens += int(response.output_tokens or 0)
            try:
                payload = dict(response.structured_output)
                if isinstance(payload.get("action"), str):
                    payload["action"] = E9Action(payload["action"])
                decision = ITBenchInvestigationDecisionV5.model_validate(payload)
            except (ValidationError, ValueError) as error:
                surface = self._surface(memory, turn)
                validation_reason = "safe action shape was invalid"
                code = (
                    error.errors()[0].get("type", "validation_error")
                    if isinstance(error, ValidationError)
                    else "validation_error"
                )
                self._reject(
                    memory,
                    turn,
                    validation_reason,
                    str(code),
                    valid_actions=surface.actions,
                    valid_operations=surface.operations,
                )
                trace.update(
                    {
                        "decision": "ACTION_REJECTED",
                        "rejection_code": str(code),
                        "rejection_reason": validation_reason,
                    }
                )
                turns.append(trace)
                if self._stalled(memory):
                    terminal = "PROTOCOL_STALLED"
                    break
                continue

            action = decision.action.value
            surface = self._surface(memory, turn)
            target_handles = [item for item in (decision.target, *decision.targets) if item]
            operation = decision.operation or ("INCIDENT_OVERVIEW" if action == "OBSERVE" else "")
            reason: str | None = None
            code = "INVALID_ACTION"
            if action not in surface.actions:
                reason, code = (
                    f"action {action} is not valid in phase {surface.phase}",
                    "INVALID_TRANSITION",
                )
            elif any(memory.resolve(handle) is None for handle in target_handles):
                reason, code = "target is not an observable candidate handle", "UNKNOWN_CANDIDATE"
            elif action in {"OBSERVE", "INVESTIGATE"} and operation not in surface.operations:
                reason, code = (
                    "operation is not currently exposed for this phase/target",
                    "UNSUPPORTED_OPERATION",
                )
            elif action in {"HYPOTHESIZE", "REVISE"} and decision.target is None:
                reason, code = f"{action} requires one target handle", "TARGET_REQUIRED"
            elif action == "SUBMIT" and not self._submit_ready(memory, decision.targets):
                reason, code = (
                    "SUBMIT requires hypothesis-associated evidence for every target",
                    "SUBMIT_PRECONDITION",
                )
            elif action == "INVESTIGATE" and memory.has_operation(decision.target, operation):
                reason, code = "operation already completed for this target", "DUPLICATE_OPERATION"
            if reason is not None:
                self._reject(
                    memory,
                    turn,
                    reason,
                    code,
                    action=action,
                    target=decision.target,
                    operation=operation,
                    valid_actions=surface.actions,
                    valid_operations=surface.operations,
                )
                trace.update(
                    {
                        "decision": "ACTION_REJECTED",
                        "parsed_action": action,
                        "target": decision.target,
                        "operation": operation,
                        "rejection_code": code,
                        "rejection_reason": reason,
                        "valid_next_actions": list(surface.actions),
                        "valid_operations": list(surface.operations),
                    }
                )
                turns.append(trace)
                if self._stalled(memory):
                    terminal = "PROTOCOL_STALLED"
                    break
                continue

            memory.append(
                "ACTION_ACCEPTED",
                turn,
                {"action": action, "target": decision.target, "operation": operation},
            )
            trace.update(
                {
                    "parsed_action": action,
                    "target": decision.target,
                    "operation": operation,
                    "accepted": True,
                }
            )
            if action == "STOP":
                terminal = "STOP"
                memory.append(
                    "CASE_STOPPED", turn, {"reason": decision.stop_reason or "model_stop"}
                )
                trace.update({"decision": terminal, "phase_after": memory.state["current_phase"]})
                turns.append(trace)
                break
            if action == "SUBMIT":
                submitted_entities = [
                    entity
                    for handle in decision.targets
                    if (entity := memory.resolve(handle)) is not None
                ]
                for handle in decision.targets:
                    submitted_refs.extend(memory.evidence_for(handle))
                memory.append(
                    "DIAGNOSIS_SUBMITTED",
                    turn,
                    {"targets": decision.targets, "submitted_entities": submitted_entities},
                )
                terminal = "SUBMIT"
                trace.update(
                    {
                        "decision": terminal,
                        "targets": decision.targets,
                        "phase_after": memory.state["current_phase"],
                    }
                )
                turns.append(trace)
                break
            if action == "HYPOTHESIZE":
                memory.set_candidate_status(
                    turn=turn,
                    handle=decision.target or "",
                    status="ACTIVE",
                    rationale=decision.rationale or "",
                    reconsider=True,
                )
                memory.append(
                    "HYPOTHESIS_PROPOSED",
                    turn,
                    {"entity_handle": decision.target, "rationale": decision.rationale or ""},
                )
                trace["decision"] = action
            elif action == "REVISE":
                memory.set_candidate_status(
                    turn=turn,
                    handle=decision.target or "",
                    status="ACTIVE",
                    rationale=decision.rationale or "",
                    reconsider=True,
                )
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
                trace["decision"] = action
            else:
                observation = operations.execute(operation, decision.target, turn)
                trace.update(
                    {"decision": action, "evidence_created": [observation["evidence_ref"]]}
                )
            trace["phase_after"] = memory.state["current_phase"]
            turns.append(trace)
        else:
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

    def _surface(self, memory: E9CaseMemory, turn: int) -> Any:
        return control_surface(
            memory.state,
            turn=turn,
            max_steps=self.limits.max_model_calls,
            max_rejections=self.max_consecutive_rejected_actions,
            semantic_limit=self.limits.max_tool_calls,
        )

    @staticmethod
    def _submit_ready(memory: E9CaseMemory, targets: list[str]) -> bool:
        return bool(
            memory.state.get("current_hypothesis")
            and targets
            and all(memory.evidence_for(handle) for handle in targets)
        )

    def _stalled(self, memory: E9CaseMemory) -> bool:
        return (
            int(memory.state.get("consecutive_rejections", 0))
            > self.max_consecutive_rejected_actions
        )

    @staticmethod
    def _reject(
        memory: E9CaseMemory,
        turn: int,
        reason: str,
        code: str,
        *,
        action: str | None = None,
        target: str | None = None,
        operation: str | None = None,
        valid_actions: tuple[str, ...] = (),
        valid_operations: tuple[str, ...] = (),
    ) -> None:
        memory.append(
            "ACTION_REJECTED",
            turn,
            {
                "attempted_action": action,
                "attempted_target": target,
                "attempted_operation": operation,
                "reason": reason[:300],
                "code": code,
                "valid_actions": list(valid_actions)[:8],
                "valid_operations": list(valid_operations)[:12],
            },
        )


__all__ = [
    "E9Limits",
    "E9InvestigationRuntime",
    "ITBENCH_E9_PROMPT",
    "ITBENCH_E9_PROMPT_VERSION",
    "e9_prompt_hash",
]
