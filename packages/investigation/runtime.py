"""Deterministic, credit-bounded single-agent investigation loop."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from packages.contracts import Evidence, Incident, TimeWindow
from packages.evidence import EvidenceService
from packages.investigation.audit import InvestigationAuditRecord, InvestigationAuditSink
from packages.investigation.context import CompactContextBuilder
from packages.investigation.contracts import (
    InvestigationDecision,
    InvestigationLimits,
    InvestigationResult,
    InvestigationUsage,
    TerminationReason,
)
from packages.investigation.prompt import INVESTIGATOR_PROMPT, investigator_prompt_hash
from packages.investigation.registry import ReadOnlyToolRegistry, RegisteredTool
from packages.provider import ModelMessage, ModelProvider, ModelRequest, ProviderError
from packages.tools import BoundedToolExecutor, ToolFailure, ToolResponse


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

    def run(self, incident: Incident) -> InvestigationResult:
        """Investigate one incident while keeping evidence authority in the runtime."""
        run_id = uuid4()
        started = monotonic()
        evidence: list[Evidence] = []
        model_calls = 0
        tool_calls = 0
        input_tokens = 0
        output_tokens = 0
        termination = TerminationReason.AGENT_STOPPED
        error_code: str | None = None
        hypothesis = None

        for _turn in range(self._limits.max_agent_turns):
            if monotonic() - started > self._limits.max_wall_time_seconds:
                termination = TerminationReason.WALL_TIME_LIMIT
                break
            if model_calls >= self._limits.max_model_calls:
                termination = TerminationReason.MODEL_CALL_LIMIT
                break

            try:
                response = self._complete(run_id, incident, evidence, model_calls, tool_calls)
                model_calls += 1
                input_tokens += response.input_tokens
                output_tokens += response.output_tokens
                decision = InvestigationDecision.model_validate_json(
                    json.dumps(response.structured_output)
                )
            except ProviderError as error:
                model_calls += 1
                termination = TerminationReason.PROVIDER_ERROR
                error_code = error.code.value
                break
            except (ValidationError, ValueError):
                model_calls += 1
                termination = TerminationReason.INVALID_DECISION
                error_code = "INVALID_DECISION"
                break

            if decision.hypothesis is not None:
                referenced = set(decision.hypothesis.evidence_ids)
                available = {item.evidence_id for item in evidence}
                if not referenced.issubset(available):
                    termination = TerminationReason.INVALID_DECISION
                    error_code = "FABRICATED_EVIDENCE_REFERENCE"
                else:
                    hypothesis = decision.hypothesis
                    termination = TerminationReason.HYPOTHESIS_SUBMITTED
                break

            if not decision.requests:
                termination = TerminationReason.AGENT_STOPPED
                break
            if len(decision.requests) > self._limits.max_tools_per_turn:
                termination = TerminationReason.TOOL_CALL_LIMIT
                error_code = "TOOL_BATCH_LIMIT_EXCEEDED"
                break
            remaining = self._limits.max_tool_calls - tool_calls
            if len(decision.requests) > remaining:
                termination = TerminationReason.TOOL_CALL_LIMIT
                error_code = "TOOL_CALL_LIMIT"
                break

            try:
                new_evidence = self._execute_tools(incident, decision.requests)
            except (PermissionError, ValueError):
                termination = TerminationReason.INVALID_DECISION
                error_code = "UNKNOWN_OR_FORBIDDEN_TOOL"
                break
            tool_calls += len(decision.requests)
            evidence.extend(new_evidence)

        if (
            termination is TerminationReason.AGENT_STOPPED
            and model_calls >= self._limits.max_model_calls
        ):
            termination = TerminationReason.MODEL_CALL_LIMIT

        usage = InvestigationUsage(
            model_calls=model_calls,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=int((monotonic() - started) * 1000),
            prompt_hash=investigator_prompt_hash(),
            provider=getattr(self._provider, "provider_name", "unknown"),
            model=self._model,
            reasoning_effort=self._reasoning_effort,
            estimated_api_calls=model_calls,
            actual_api_calls=model_calls
            if getattr(self._provider, "provider_name", "unknown") == "openai"
            else 0,
        )
        result = InvestigationResult(
            run_id=run_id,
            incident_id=incident.incident_id,
            hypothesis=hypothesis,
            evidence=evidence,
            usage=usage,
            termination_reason=termination,
            error_code=error_code,
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
                    termination_reason=result.termination_reason,
                    recorded_at=datetime.now(UTC),
                )
            )
        return result

    def _complete(
        self,
        run_id: UUID,
        incident: Incident,
        evidence: list[Evidence],
        model_calls: int,
        tool_calls: int,
    ) -> Any:
        """Build a bounded request with the versioned structured-output schema."""
        context = self._context_builder.build(
            incident,
            evidence,
            self._registry.names(),
            model_calls_remaining=self._limits.max_model_calls - model_calls,
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
        )
        return self._provider.complete(request)

    def _execute_tools(self, incident: Incident, requests: list[Any]) -> list[Evidence]:
        """Resolve and execute a batch concurrently, then normalize successes."""
        resolved: list[tuple[RegisteredTool, Any]] = []
        for request in requests:
            tool = self._registry.get(request.tool)
            resolved.append((tool, tool.request(incident.incident_id, request.arguments)))

        outputs: list[tuple[RegisteredTool, Any]] = []
        with ThreadPoolExecutor(max_workers=len(resolved)) as pool:
            futures = [
                pool.submit(self._tool_executor.execute, tool.tool, call) for tool, call in resolved
            ]
            for future, pair in zip(futures, resolved, strict=True):
                outputs.append((pair[0], future.result()))

        collected_at = datetime.now(UTC)
        window = TimeWindow(starts_at=incident.created_at, ends_at=incident.updated_at)
        normalized: list[Evidence] = []
        for tool, result in outputs:
            call_id = result.tool_call_id
            self._evidence_service.register_tool_call(call_id, incident.incident_id)
            if isinstance(result, ToolFailure):
                continue
            if not isinstance(result, ToolResponse):
                continue
            evidence = Evidence(
                incident_id=incident.incident_id,
                source_type=tool.source_type,
                source_system=tool.name,
                observation=result.data,
                time_window=window,
                tool_call_id=call_id,
                raw_result_reference=f"{tool.name}://{call_id}",
                collected_at=collected_at,
            )
            normalized.append(self._evidence_service.add(evidence))
        return normalized
