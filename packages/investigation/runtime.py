"""Deterministic, credit-bounded single-agent investigation loop."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from packages.contracts import Alert, Evidence, Incident
from packages.evidence import EvidenceService
from packages.investigation.audit import (
    InvestigationAuditRecord,
    InvestigationAuditSink,
    InvestigationTurnAudit,
)
from packages.investigation.context import (
    CompactContextBuilder,
    InvestigationObservationWindow,
    derive_observation_window,
)
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
    ToolRepeatPolicy,
    ValidationStage,
)
from packages.investigation.duplicates import (
    ToolObservationHistory,
    ToolRequestIdentity,
    make_tool_request_identity,
)
from packages.investigation.prompt import INVESTIGATOR_PROMPT, investigator_prompt_hash
from packages.investigation.registry import ReadOnlyToolRegistry, RegisteredTool
from packages.investigation.tool_contracts import ToolArgumentValidationError
from packages.provider import (
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ProviderAccountingSnapshot,
    ProviderError,
    ToolSchemaDescriptor,
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


@dataclass(frozen=True, slots=True)
class _PreparedToolRequest:
    """One validated request with its canonical reusable identity."""

    request: Any
    registered: RegisteredTool
    canonical_arguments: dict[str, Any]
    identity: ToolRequestIdentity


class _ToolBudgetExceeded(ValueError):
    """Internal typed boundary for a novel batch larger than remaining budget."""

    def __init__(self, audits: list[dict[str, Any]]) -> None:
        super().__init__("tool batch exceeds remaining incident budget")
        self.audits = audits


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
        validation_stage: ValidationStage | None = None
        validation_path: str | None = None
        validator: str | None = None
        hypothesis = None
        terminal_decision: DecisionType | None = None
        stop_reason: StopReason | None = None
        progress: list[dict[str, Any]] = []
        observation_history: dict[str, ToolObservationHistory] = {}
        tool_requests_total = 0
        duplicate_requests_suppressed = 0
        turns: list[dict[str, Any]] = []
        observation_window = derive_observation_window(incident, alerts)

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
                    observation_window,
                )
                input_tokens += response.input_tokens
                output_tokens += response.output_tokens
                decision = InvestigationDecision.model_validate_json(
                    json.dumps(response.structured_output)
                )
            except ProviderError as error:
                termination = TerminationReason.PROVIDER_ERROR
                error_code = error.code.value
                validation_stage = (
                    ValidationStage.PROVIDER_FUNCTION_ARGUMENTS
                    if "FUNCTION" in error.code.value or "DECISION" in error.code.value
                    else ValidationStage.PROVIDER_ENVELOPE
                )
                validation_path = error.metadata.schema_error_path if error.metadata else None
                validator = "OpenAIResponsesAdapter"
                turns.append(
                    self._turn_summary(
                        logical_model_turns,
                        response,
                        error_code,
                        validation_stage,
                        validation_path,
                    )
                )
                break
            except ValidationError as error:
                termination = TerminationReason.INVALID_DECISION
                error_code = _semantic_error_code(
                    getattr(response, "structured_output", None), self._limits
                )
                validation_stage = ValidationStage.DECISION_SCHEMA
                validation_path = self._validation_path(error)
                validator = "InvestigationDecision"
                turns.append(
                    self._turn_summary(
                        logical_model_turns,
                        response,
                        error_code,
                        validation_stage,
                        validation_path,
                    )
                )
                break

            if decision.hypothesis is not None:
                referenced = set(decision.hypothesis.evidence_ids)
                available = {item.evidence_id for item in evidence}
                if not referenced.issubset(available):
                    termination = TerminationReason.INVALID_DECISION
                    error_code = InvestigationErrorCode.FABRICATED_EVIDENCE_REFERENCE.value
                    validation_stage = ValidationStage.EVIDENCE_PROVENANCE
                    validation_path = "$.hypothesis.evidence_ids"
                    validator = "EvidenceService"
                else:
                    hypothesis = decision.hypothesis
                    terminal_decision = DecisionType.SUBMIT_HYPOTHESIS
                    termination = TerminationReason.HYPOTHESIS_SUBMITTED
                turns.append(
                    self._turn_summary(
                        logical_model_turns,
                        response,
                        "PASS" if terminal_decision else error_code or "FAIL",
                        validation_stage,
                        validation_path,
                        decision=decision,
                    )
                )
                break

            if decision.decision is DecisionType.STOP:
                terminal_decision = DecisionType.STOP
                stop_reason = decision.stop_reason
                termination = TerminationReason.AGENT_STOPPED
                turns.append(
                    self._turn_summary(
                        logical_model_turns,
                        response,
                        "PASS",
                        None,
                        None,
                        decision=decision,
                    )
                )
                break
            tool_requests_total += len(decision.requests)

            try:
                new_evidence, summaries, attempted, suppressed = self._execute_tools(
                    incident,
                    decision.requests,
                    observation_window,
                    turn=logical_model_turns,
                    tool_calls_used=tool_calls,
                    history=observation_history,
                )
            except PermissionError:
                termination = TerminationReason.INVALID_DECISION
                error_code = InvestigationErrorCode.UNKNOWN_INVESTIGATION_TOOL.value
                validation_stage = ValidationStage.TOOL_REGISTRY
                validation_path = "$.requests[].tool"
                validator = "ReadOnlyToolRegistry"
                turns.append(
                    self._turn_summary(
                        logical_model_turns,
                        response,
                        "FAIL",
                        validation_stage,
                        validation_path,
                        decision=decision,
                    )
                )
                break
            except ToolArgumentValidationError as error:
                termination = TerminationReason.INVALID_DECISION
                error_code = InvestigationErrorCode.TOOL_ARGUMENT_SCHEMA_INVALID.value
                validation_stage = ValidationStage.TOOL_ARGUMENTS
                validation_path = error.path
                validator = "RegisteredTool.argument_model"
                turns.append(
                    self._turn_summary(
                        logical_model_turns,
                        response,
                        "FAIL",
                        validation_stage,
                        validation_path,
                        decision=decision,
                    )
                )
                break
            except _ToolBudgetExceeded as error:
                termination = TerminationReason.TOOL_CALL_LIMIT
                error_code = InvestigationErrorCode.TOOL_BUDGET_EXCEEDED.value
                validation_stage = ValidationStage.TOOL_BUDGET
                validation_path = "$.requests"
                validator = "InvestigationLimits"
                turns.append(
                    self._turn_summary(
                        logical_model_turns,
                        response,
                        "FAIL",
                        validation_stage,
                        validation_path,
                        decision=decision,
                        request_audits=error.audits,
                    )
                )
                break
            except ValueError:
                termination = TerminationReason.INVALID_DECISION
                error_code = InvestigationErrorCode.INVALID_TOOL_ARGUMENTS.value
                validation_stage = ValidationStage.TOOL_EXECUTION
                validation_path = "$.requests"
                validator = "BoundedToolExecutor"
                break
            failed_summary = next(
                (
                    item
                    for item in summaries
                    if item.get("status") not in {"SUCCESS", "SKIPPED_DUPLICATE"}
                ),
                None,
            )
            # Account for every dispatch and retain all successful evidence
            # before classifying any sibling tool failure.
            tool_calls += attempted
            evidence.extend(new_evidence)
            progress.extend(summaries)
            duplicate_requests_suppressed += suppressed
            if failed_summary is not None:
                termination = TerminationReason.TOOL_FAILURE
                error_code = str(failed_summary.get("error_code", "TOOL_EXECUTION_FAILED"))
                validation_stage = ValidationStage.TOOL_EXECUTION
                validation_path = "$.requests"
                validator = "BoundedToolExecutor"
                turns.append(
                    self._turn_summary(
                        logical_model_turns,
                        response,
                        "FAIL",
                        validation_stage,
                        validation_path,
                        decision=decision,
                        summaries=summaries,
                        request_audits=summaries,
                        tool_calls_attempted=attempted,
                        tool_calls_succeeded=sum(
                            1 for item in summaries if item.get("status") == "SUCCESS"
                        ),
                        tool_calls_failed=sum(
                            1
                            for item in summaries
                            if item.get("status") not in {"SUCCESS", "SKIPPED_DUPLICATE"}
                        ),
                        duplicate_requests_suppressed=suppressed,
                    )
                )
                break
            turns.append(
                self._turn_summary(
                    logical_model_turns,
                    response,
                    "PASS",
                    None,
                    None,
                    decision=decision,
                    summaries=summaries,
                    request_audits=summaries,
                    tool_calls_attempted=attempted,
                    tool_calls_succeeded=sum(
                        1 for item in summaries if item.get("status") == "SUCCESS"
                    ),
                    tool_calls_failed=sum(
                        1
                        for item in summaries
                        if item.get("status") not in {"SUCCESS", "SKIPPED_DUPLICATE"}
                    ),
                    duplicate_requests_suppressed=suppressed,
                )
            )

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
            tool_requests_total=tool_requests_total,
            duplicate_requests_suppressed=duplicate_requests_suppressed,
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
            validation_stage=validation_stage,
            validation_path=validation_path,
            validator=validator,
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
            validation_stage=validation_stage,
            validation_path=validation_path,
            validator=validator,
            turns=turns,
        )
        if self._audit_sink is not None:
            record_turn = getattr(self._audit_sink, "record_turn", None)
            if callable(record_turn):
                for item in turns:
                    allowed = (
                        ["SUBMIT_HYPOTHESIS", "STOP"]
                        if item["turn"] >= self._limits.max_model_calls
                        else ["CALL_TOOLS", "SUBMIT_HYPOTHESIS", "STOP"]
                    )
                    record_turn(
                        InvestigationTurnAudit(
                            run_id=run_id,
                            incident_id=incident.incident_id,
                            turn_number=item["turn"],
                            current_model_call=item["turn"],
                            future_model_calls_after_decision=max(
                                self._limits.max_model_calls - item["turn"], 0
                            ),
                            is_final_model_turn=item["turn"] >= self._limits.max_model_calls,
                            allowed_decisions=allowed,
                            selected_decision_function=item.get("selected_decision"),
                            requested_tool_count=item.get("requested_tool_count", 0),
                            requested_tool_names=item.get("requested_tool_names", []),
                            requested_tool_argument_keys=[
                                summary.get("argument_keys", [])
                                for summary in item.get("summaries", [])
                            ],
                            requested_tool_argument_types=[
                                summary.get("argument_types", {})
                                for summary in item.get("summaries", [])
                            ],
                            requested_tool_argument_hashes=[
                                summary.get("argument_value_hashes", {})
                                for summary in item.get("summaries", [])
                            ],
                            request_audits=item.get("request_audits", []),
                            duplicate_requests_suppressed=item.get(
                                "duplicate_requests_suppressed", 0
                            ),
                            model_input_tokens=item.get("input_tokens", 0),
                            model_output_tokens=item.get("output_tokens", 0),
                            validation_stage=(
                                ValidationStage(item["validation_stage"])
                                if item.get("validation_stage")
                                else None
                            ),
                            validation_result=item.get("validation_result", "NOT_EVALUATED"),
                            error_path=item.get("error_path"),
                            error_code=(
                                result.error_code
                                if item.get("validation_result") != "PASS"
                                else None
                            ),
                            tool_calls_attempted=item.get("tool_calls_attempted", 0),
                            tool_calls_succeeded=item.get("tool_calls_succeeded", 0),
                            tool_calls_failed=item.get("tool_calls_failed", 0),
                            evidence_ids_created=[
                                evidence_id
                                for summary in item.get("summaries", [])
                                if summary.get("status") == "SUCCESS"
                                for evidence_id in summary.get("evidence_ids", [])
                            ],
                            recorded_at=datetime.now(UTC),
                        )
                    )
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
                    tool_requests_total=usage.tool_requests_total,
                    duplicate_requests_suppressed=usage.duplicate_requests_suppressed,
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
                    validation_stage=result.validation_stage,
                    validation_path=result.validation_path,
                    validator=result.validator,
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
        observation_window: InvestigationObservationWindow,
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
            observation_window=observation_window,
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
            allowed_decisions=(
                ("SUBMIT_HYPOTHESIS", "STOP")
                if model_calls >= self._limits.max_model_calls
                else (
                    "CALL_TOOLS",
                    "SUBMIT_HYPOTHESIS",
                    "STOP",
                )
            ),
            allowed_tool_names=self._registry.names(),
            tool_schemas=tuple(
                ToolSchemaDescriptor(
                    name=descriptor["name"],
                    arguments=descriptor["arguments"],
                )
                for descriptor in self._registry.descriptors()
            ),
        )
        return self._provider.complete(request)

    def _execute_tools(
        self,
        incident: Incident,
        requests: list[Any],
        observation_window: InvestigationObservationWindow,
        *,
        turn: int,
        tool_calls_used: int,
        history: dict[str, ToolObservationHistory],
    ) -> tuple[list[Evidence], list[dict[str, Any]], int, int]:
        """Suppress reusable duplicates and execute only novel requests."""
        scope = {
            "starts_at": observation_window.starts_at.isoformat(),
            "ends_at": observation_window.ends_at.isoformat(),
            "temporal_mode": observation_window.temporal_mode,
        }
        prepared: list[_PreparedToolRequest] = []
        for request in requests:
            registered = self._registry.get(request.tool)
            canonical = registered.validate_arguments(request.arguments)
            prepared.append(
                _PreparedToolRequest(
                    request=request,
                    registered=registered,
                    canonical_arguments=canonical,
                    identity=make_tool_request_identity(registered.name, canonical, scope),
                )
            )

        novel: list[tuple[int, _PreparedToolRequest]] = []
        duplicate_indices: list[tuple[int, _PreparedToolRequest, str]] = []
        seen_in_turn: set[str] = set()
        for index, item in enumerate(prepared):
            identity_hash = item.identity.identity_hash
            prior = history.get(identity_hash)
            same_turn = identity_hash in seen_in_turn
            suppress = same_turn or (
                item.registered.repeat_policy is ToolRepeatPolicy.FIXED_WINDOW
                and prior is not None
                and prior.status == "SUCCESS"
            )
            if suppress:
                duplicate_indices.append((index, item, "SAME_TURN" if same_turn else "PRIOR_TURN"))
            else:
                novel.append((index, item))
                seen_in_turn.add(identity_hash)

        if len(novel) > self._limits.max_tool_calls - tool_calls_used:
            raise _ToolBudgetExceeded(
                [self._request_audit(item, "REJECTED_BUDGET") for item in prepared]
            )

        resolved: list[tuple[int, _PreparedToolRequest, Any]] = []
        for index, item in novel:
            call = item.registered.request(
                incident.incident_id,
                item.canonical_arguments,
                observation_window={
                    "starts_at": observation_window.starts_at.isoformat(),
                    "ends_at": observation_window.ends_at.isoformat(),
                },
                temporal_mode=observation_window.temporal_mode,
            )
            resolved.append((index, item, call))

        outputs: list[tuple[int, _PreparedToolRequest, Any]] = []
        if resolved:
            with ThreadPoolExecutor(max_workers=len(resolved)) as pool:
                futures = [
                    pool.submit(self._tool_executor.execute, item.registered.tool, call)
                    for _index, item, call in resolved
                ]
                for future, pair in zip(futures, resolved, strict=True):
                    outputs.append((pair[0], pair[1], future.result()))

        collected_at = datetime.now(UTC)
        window = observation_window.time_window()
        normalized: list[Evidence] = []
        summaries_by_index: dict[int, dict[str, Any]] = {}
        for index, item, result in outputs:
            call_id = result.tool_call_id
            self._evidence_service.register_tool_call(call_id, incident.incident_id)
            if isinstance(result, ToolFailure):
                summary = {
                    "tool": item.registered.name,
                    **self._safe_arguments(item.canonical_arguments),
                    "status": result.code.value,
                    "error_code": result.code.value,
                    "backend": result.backend,
                    "operation": result.operation,
                    "http_status": result.http_status,
                    "evidence_ids": [],
                    "identity_hash": item.identity.identity_hash,
                    "repeat_policy": item.registered.repeat_policy.value,
                }
                summaries_by_index[index] = summary
                history[item.identity.identity_hash] = ToolObservationHistory(
                    identity=item.identity,
                    repeat_policy=item.registered.repeat_policy,
                    turn=turn,
                    status=result.code.value,
                    evidence_ids=(),
                    tool_call_id=str(call_id),
                    result_count=0,
                )
                continue
            if not isinstance(result, ToolResponse):
                continue
            evidence = Evidence(
                incident_id=incident.incident_id,
                source_type=item.registered.source_type,
                source_system=item.registered.name,
                observation=result.data,
                time_window=result.effective_time_window or window,
                tool_call_id=call_id,
                raw_result_reference=f"{item.registered.name}://{call_id}",
                collected_at=collected_at,
            )
            if result.temporal_mode:
                evidence.observation = {
                    **evidence.observation,
                    "temporal_mode": result.temporal_mode,
                }
            normalized.append(self._evidence_service.add(evidence))
            evidence_ids = (str(normalized[-1].evidence_id),)
            summaries_by_index[index] = {
                "tool": item.registered.name,
                **self._safe_arguments(item.canonical_arguments),
                "status": "SUCCESS",
                "evidence_ids": list(evidence_ids),
                "identity_hash": item.identity.identity_hash,
                "repeat_policy": item.registered.repeat_policy.value,
            }
            history[item.identity.identity_hash] = ToolObservationHistory(
                identity=item.identity,
                repeat_policy=item.registered.repeat_policy,
                turn=turn,
                status="SUCCESS",
                evidence_ids=evidence_ids,
                tool_call_id=str(call_id),
                result_count=result.result_count,
            )

        for index, item, duplicate_scope in duplicate_indices:
            prior = history.get(item.identity.identity_hash)
            summaries_by_index[index] = {
                "tool": item.registered.name,
                **self._safe_arguments(item.canonical_arguments),
                "status": "SKIPPED_DUPLICATE",
                "duplicate_scope": duplicate_scope,
                "original_turn": prior.turn if prior else turn,
                "original_tool_call_id": prior.tool_call_id if prior else None,
                "reused_evidence_ids": list(prior.evidence_ids) if prior else [],
                "evidence_ids": list(prior.evidence_ids) if prior else [],
                "identity_hash": item.identity.identity_hash,
                "repeat_policy": item.registered.repeat_policy.value,
            }

        return (
            normalized,
            [summaries_by_index[index] for index in range(len(prepared))],
            len(novel),
            len(duplicate_indices),
        )

    def _request_audit(self, item: _PreparedToolRequest, status: str) -> dict[str, Any]:
        """Return a safe pre-execution request record."""
        return {
            "tool": item.registered.name,
            **self._safe_arguments(item.canonical_arguments),
            "status": status,
            "identity_hash": item.identity.identity_hash,
            "repeat_policy": item.registered.repeat_policy.value,
        }

    @staticmethod
    def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        """Expose argument shape and hashes without retaining free-form values."""
        safe_values = {
            key: value
            for key, value in arguments.items()
            if key in {"service", "consumer", "deployment", "trace_id", "range_seconds"}
        }
        return {
            "argument_keys": sorted(arguments),
            "argument_types": {key: type(arguments[key]).__name__ for key in sorted(arguments)},
            "argument_value_hashes": {
                key: sha256(
                    json.dumps(
                        arguments[key], sort_keys=True, separators=(",", ":"), default=str
                    ).encode()
                ).hexdigest()
                for key in sorted(arguments)
            },
            "arguments": safe_values,
        }

    @staticmethod
    def _validation_path(error: ValidationError) -> str:
        """Convert a Pydantic location into a safe JSON path."""
        errors = error.errors()
        if not errors:
            return "$"
        location = ".".join(str(item) for item in errors[0].get("loc", ()))
        return f"$.{location}" if location else "$"

    @staticmethod
    def _turn_summary(
        turn: int,
        response: Any,
        result: str,
        stage: ValidationStage | None,
        path: str | None,
        *,
        decision: InvestigationDecision | None = None,
        summaries: list[dict[str, Any]] | None = None,
        request_audits: list[dict[str, Any]] | None = None,
        tool_calls_attempted: int = 0,
        tool_calls_succeeded: int = 0,
        tool_calls_failed: int = 0,
        duplicate_requests_suppressed: int = 0,
    ) -> dict[str, Any]:
        """Return bounded turn data safe for smoke/benchmark artifacts."""
        names = [item.tool for item in (decision.requests if decision else [])]
        metadata = getattr(response, "response_metadata", None)
        function_names = getattr(metadata, "function_call_names", []) or []
        return {
            "turn": turn,
            "selected_decision": function_names[0]
            if function_names
            else (decision.decision.value if decision else None),
            "requested_tool_count": len(decision.requests) if decision else 0,
            "requested_tool_names": names,
            "summaries": summaries or [],
            "request_audits": request_audits or summaries or [],
            "tool_calls_attempted": tool_calls_attempted,
            "tool_calls_succeeded": tool_calls_succeeded,
            "tool_calls_failed": tool_calls_failed,
            "duplicate_requests_suppressed": duplicate_requests_suppressed,
            "validation_result": result,
            "validation_stage": stage.value if stage else None,
            "error_path": path,
            "input_tokens": getattr(response, "input_tokens", 0),
            "output_tokens": getattr(response, "output_tokens", 0),
        }
