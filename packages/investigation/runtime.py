"""Deterministic, credit-bounded single-agent investigation loop."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from packages.contracts import Alert, Evidence, Incident, TimeWindow
from packages.evidence import EvidenceService
from packages.investigation.audit import InvestigationAuditRecord, InvestigationAuditSink
from packages.investigation.context import CompactContextBuilder
from packages.investigation.contracts import (
    DecisionType,
    HypothesisMechanism,
    InvestigationDecision,
    InvestigationErrorCode,
    InvestigationLimits,
    InvestigationResult,
    InvestigationUsage,
    StopReason,
    TerminationReason,
)
from packages.investigation.prompt import INVESTIGATOR_PROMPT, investigator_prompt_hash
from packages.investigation.registry import ReadOnlyToolRegistry, RegisteredTool
from packages.provider import (
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ProviderAccountingSnapshot,
    ProviderError,
)
from packages.tools import BoundedToolExecutor, ToolFailure, ToolResponse


def _semantic_error_code(payload: Any, limits: InvestigationLimits) -> str:
    """Classify model contract failures without weakening the domain validator."""
    if not isinstance(payload, dict):
        return InvestigationErrorCode.INVALID_DECISION.value
    decision = payload.get("decision")
    if decision == DecisionType.CALL_TOOLS:
        requests = payload.get("requests")
        if not isinstance(requests, list) or not requests:
            return InvestigationErrorCode.EMPTY_TOOL_REQUESTS.value
        if len(requests) > limits.max_tool_calls:
            return InvestigationErrorCode.TOOL_BUDGET_EXCEEDED.value
        for request in requests:
            if not isinstance(request, dict) or not isinstance(request.get("arguments", {}), dict):
                return InvestigationErrorCode.INVALID_TOOL_ARGUMENTS.value
    elif decision == DecisionType.SUBMIT_HYPOTHESIS:
        hypothesis = payload.get("hypothesis")
        if not isinstance(hypothesis, dict):
            return InvestigationErrorCode.INVALID_HYPOTHESIS_SHAPE.value
        evidence_ids = hypothesis.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            return InvestigationErrorCode.EMPTY_EVIDENCE_SET.value
        if hypothesis.get("mechanism") not in {item.value for item in HypothesisMechanism}:
            return InvestigationErrorCode.UNKNOWN_HYPOTHESIS_MECHANISM.value
    elif decision == DecisionType.STOP:
        if payload.get("stop_reason") not in {item.value for item in StopReason}:
            return InvestigationErrorCode.INVALID_STOP_REASON.value
    return InvestigationErrorCode.INVALID_DECISION.value


def _has_duplicate_tool_requests(requests: list[Any]) -> bool:
    """Reject exact duplicate tool requests without silently deduplicating them."""
    seen: set[tuple[str, str]] = set()
    for request in requests:
        if not hasattr(request, "tool") or not hasattr(request, "arguments"):
            continue
        key = (
            request.tool,
            json.dumps(request.arguments, sort_keys=True, separators=(",", ":"), default=str),
        )
        if key in seen:
            return True
        seen.add(key)
    return False


class InvestigationRuntime:
    """Run at most three model turns and eight bounded read-only tool calls."""

    def __init__(
        self,
        provider: ModelProvider,
        registry: ReadOnlyToolRegistry,
        *,
        tool_executor: BoundedToolExecutor | None = None,
        evidence_service: EvidenceService | None = None,
        model: str = "gpt-5.6-luna",
        reasoning_effort: str = "none",
        limits: InvestigationLimits | None = None,
        audit_sink: InvestigationAuditSink | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._tool_executor = tool_executor or BoundedToolExecutor()
        self._evidence_service = evidence_service or EvidenceService()
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._limits = limits or InvestigationLimits()
        self._context_builder = CompactContextBuilder()
        self._audit_sink = audit_sink

    def run(self, incident: Incident, alerts: tuple[Alert, ...] = ()) -> InvestigationResult:
        """Investigate one incident while keeping evidence authority in the runtime."""
        run_id = uuid4()
        started = monotonic()
        accounting_reader = getattr(self._provider, "accounting_snapshot", None)
        accounting_before = accounting_reader() if callable(accounting_reader) else None
        evidence: list[Evidence] = []
        logical_model_turns = 0
        tool_calls = 0
        input_tokens = 0
        output_tokens = 0
        termination = TerminationReason.AGENT_STOPPED
        error_code: str | None = None
        hypothesis = None
        terminal_decision: DecisionType | None = None
        stop_reason: StopReason | None = None
        progress: list[dict[str, Any]] = []
        requested_tool_keys: set[tuple[str, str]] = set()

        for _turn in range(self._limits.max_agent_turns):
            if monotonic() - started > self._limits.max_wall_time_seconds:
                termination = TerminationReason.WALL_TIME_LIMIT
                break
            if logical_model_turns >= self._limits.max_model_calls:
                termination = TerminationReason.MODEL_CALL_LIMIT
                break

            logical_model_turns += 1
            response: Any = None
            try:
                response = self._complete(
                    run_id,
                    incident,
                    alerts,
                    evidence,
                    logical_model_turns,
                    tool_calls,
                    progress,
                )
                input_tokens += response.input_tokens
                output_tokens += response.output_tokens
                decision = InvestigationDecision.model_validate_json(
                    json.dumps(response.structured_output)
                )
            except ProviderError as error:
                termination = TerminationReason.PROVIDER_ERROR
                error_code = error.code.value
                break
            except (ValidationError, ValueError):
                termination = TerminationReason.INVALID_DECISION
                error_code = _semantic_error_code(
                    getattr(response, "structured_output", None), self._limits
                )
                break

            if decision.hypothesis is not None:
                referenced = set(decision.hypothesis.evidence_ids)
                available = {item.evidence_id for item in evidence}
                if not referenced.issubset(available):
                    termination = TerminationReason.INVALID_DECISION
                    error_code = InvestigationErrorCode.FABRICATED_EVIDENCE_REFERENCE.value
                else:
                    hypothesis = decision.hypothesis
                    terminal_decision = DecisionType.SUBMIT_HYPOTHESIS
                    termination = TerminationReason.HYPOTHESIS_SUBMITTED
                break

            if decision.decision is DecisionType.STOP:
                terminal_decision = DecisionType.STOP
                stop_reason = decision.stop_reason
                termination = TerminationReason.AGENT_STOPPED
                break
            remaining = self._limits.max_tool_calls - tool_calls
            if len(decision.requests) > remaining:
                termination = TerminationReason.TOOL_CALL_LIMIT
                error_code = InvestigationErrorCode.TOOL_BUDGET_EXCEEDED.value
                break
            if _has_duplicate_tool_requests(decision.requests) or any(
                (
                    request.tool,
                    json.dumps(
                        request.arguments, sort_keys=True, separators=(",", ":"), default=str
                    ),
                )
                in requested_tool_keys
                for request in decision.requests
            ):
                termination = TerminationReason.INVALID_DECISION
                error_code = InvestigationErrorCode.DUPLICATE_TOOL_REQUEST.value
                break

            try:
                new_evidence, summaries = self._execute_tools(incident, decision.requests)
            except PermissionError:
                termination = TerminationReason.INVALID_DECISION
                error_code = InvestigationErrorCode.UNKNOWN_INVESTIGATION_TOOL.value
                break
            except ValueError:
                termination = TerminationReason.INVALID_DECISION
                error_code = InvestigationErrorCode.INVALID_TOOL_ARGUMENTS.value
                break
            tool_calls += len(decision.requests)
            evidence.extend(new_evidence)
            requested_tool_keys.update(
                (
                    request.tool,
                    json.dumps(
                        request.arguments, sort_keys=True, separators=(",", ":"), default=str
                    ),
                )
                for request in decision.requests
            )
            progress.extend(summaries)

        if (
            termination is TerminationReason.AGENT_STOPPED
            and terminal_decision is None
            and logical_model_turns >= self._limits.max_model_calls
        ):
            termination = TerminationReason.MODEL_CALL_LIMIT

        model_budget_exhausted_after_terminal_decision = (
            terminal_decision is not None and logical_model_turns >= self._limits.max_model_calls
        )

        if callable(accounting_reader):
            current = accounting_reader()
            baseline = accounting_before or ProviderAccountingSnapshot()
            accounting = ProviderAccountingSnapshot(
                provider_invocations=current.provider_invocations - baseline.provider_invocations,
                outbound_api_attempts=current.outbound_api_attempts
                - baseline.outbound_api_attempts,
                provider_retries=current.provider_retries - baseline.provider_retries,
                shared_ledger_consumed=current.shared_ledger_consumed
                - baseline.shared_ledger_consumed,
            )
        else:
            provider_name = getattr(self._provider, "provider_name", "unknown")
            outbound = logical_model_turns if provider_name == "openai" else 0
            accounting = ProviderAccountingSnapshot(
                provider_invocations=logical_model_turns,
                outbound_api_attempts=outbound,
                shared_ledger_consumed=outbound,
            )
        provider_name = getattr(self._provider, "provider_name", "unknown")
        estimated_api_calls = (
            accounting.outbound_api_attempts if provider_name == "openai" else logical_model_turns
        )

        usage = InvestigationUsage(
            model_calls=logical_model_turns,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=int((monotonic() - started) * 1000),
            prompt_hash=investigator_prompt_hash(),
            provider=provider_name,
            model=self._model,
            reasoning_effort=self._reasoning_effort,
            estimated_api_calls=estimated_api_calls,
            actual_api_calls=accounting.outbound_api_attempts,
            logical_model_turns=logical_model_turns,
            provider_invocations=accounting.provider_invocations,
            outbound_api_attempts=accounting.outbound_api_attempts,
            provider_retries=accounting.provider_retries,
            shared_ledger_consumed=accounting.shared_ledger_consumed,
            model_calls_limit=self._limits.max_model_calls,
            terminal_decision=terminal_decision,
            stop_reason=stop_reason,
            model_budget_exhausted_after_terminal_decision=(
                model_budget_exhausted_after_terminal_decision
            ),
        )
        result = InvestigationResult(
            run_id=run_id,
            incident_id=incident.incident_id,
            hypothesis=hypothesis,
            evidence=evidence,
            usage=usage,
            termination_reason=termination,
            error_code=error_code,
            terminal_decision=terminal_decision,
            stop_reason=stop_reason,
        )
        if self._audit_sink is not None:
            self._audit_sink.record(
                InvestigationAuditRecord(
                    run_id=run_id,
                    incident_id=incident.incident_id,
                    provider=usage.provider,
                    model=usage.model,
                    reasoning_effort=usage.reasoning_effort,
                    prompt_hash=usage.prompt_hash,
                    model_calls=usage.model_calls,
                    tool_calls=usage.tool_calls,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    latency_ms=usage.latency_ms,
                    estimated_api_calls=usage.estimated_api_calls,
                    actual_api_calls=usage.actual_api_calls,
                    logical_model_turns=usage.logical_model_turns,
                    provider_invocations=usage.provider_invocations,
                    outbound_api_attempts=usage.outbound_api_attempts,
                    provider_retries=usage.provider_retries,
                    shared_ledger_consumed=usage.shared_ledger_consumed,
                    model_calls_limit=usage.model_calls_limit,
                    terminal_decision=usage.terminal_decision,
                    stop_reason=usage.stop_reason,
                    model_budget_exhausted_after_terminal_decision=(
                        usage.model_budget_exhausted_after_terminal_decision
                    ),
                    termination_reason=result.termination_reason,
                    recorded_at=datetime.now(UTC),
                )
            )
        return result

    def _complete(
        self,
        run_id: UUID,
        incident: Incident,
        alerts: tuple[Alert, ...],
        evidence: list[Evidence],
        model_calls: int,
        tool_calls: int,
        progress: list[dict[str, Any]],
    ) -> Any:
        """Build a bounded request with the versioned structured-output schema."""
        context = self._context_builder.build(
            incident,
            evidence,
            self._registry.descriptors(),
            alerts=alerts,
            progress=tuple(progress),
            current_model_call=model_calls,
            max_model_calls=self._limits.max_model_calls,
            tool_calls_used=tool_calls,
            tool_calls_remaining=self._limits.max_tool_calls - tool_calls,
        )
        request = ModelRequest(
            run_id=run_id,
            messages=[
                ModelMessage(role="system", content=INVESTIGATOR_PROMPT),
                ModelMessage(role="user", content=context),
            ],
            response_schema_name="investigation_decision",
            response_schema=InvestigationDecision.model_json_schema(),
            model=self._model,
            reasoning_effort=self._reasoning_effort,  # type: ignore[arg-type]
            max_output_tokens=1_000,
            timeout_ms=10_000,
            allowed_decision_functions=(
                ("submit_root_cause_hypothesis", "stop_investigation")
                if model_calls >= self._limits.max_model_calls
                else (
                    "request_investigation_tools",
                    "submit_root_cause_hypothesis",
                    "stop_investigation",
                )
            ),
        )
        return self._provider.complete(request)

    def _execute_tools(
        self, incident: Incident, requests: list[Any]
    ) -> tuple[list[Evidence], list[dict[str, Any]]]:
        """Resolve and execute a batch concurrently, then normalize successes."""
        resolved: list[tuple[RegisteredTool, Any, dict[str, Any]]] = []
        for request in requests:
            tool = self._registry.get(request.tool)
            resolved.append(
                (tool, tool.request(incident.incident_id, request.arguments), request.arguments)
            )

        outputs: list[tuple[RegisteredTool, Any, dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=len(resolved)) as pool:
            futures = [
                pool.submit(self._tool_executor.execute, tool.tool, call)
                for tool, call, _arguments in resolved
            ]
            for future, pair in zip(futures, resolved, strict=True):
                outputs.append((pair[0], future.result(), pair[2]))

        collected_at = datetime.now(UTC)
        window = TimeWindow(starts_at=incident.created_at, ends_at=incident.updated_at)
        normalized: list[Evidence] = []
        summaries: list[dict[str, Any]] = []
        for tool, result, arguments in outputs:
            call_id = result.tool_call_id
            self._evidence_service.register_tool_call(call_id, incident.incident_id)
            if isinstance(result, ToolFailure):
                summaries.append(
                    {
                        "tool": tool.name,
                        "arguments": arguments,
                        "status": result.code.value,
                        "evidence_ids": [],
                    }
                )
                continue
            if not isinstance(result, ToolResponse):
                continue
            evidence = Evidence(
                incident_id=incident.incident_id,
                source_type=tool.source_type,
                source_system=tool.name,
                observation=result.data,
                time_window=result.effective_time_window or window,
                tool_call_id=call_id,
                raw_result_reference=f"{tool.name}://{call_id}",
                collected_at=collected_at,
            )
            normalized.append(self._evidence_service.add(evidence))
            summaries.append(
                {
                    "tool": tool.name,
                    "arguments": arguments,
                    "status": "SUCCESS",
                    "evidence_ids": [str(normalized[-1].evidence_id)],
                }
            )
        return normalized, summaries
