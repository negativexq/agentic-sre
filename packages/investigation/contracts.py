"""Strict structured decisions and run records for the investigator."""

from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts import Evidence


class InvestigationModel(BaseModel):
    """Shared strict boundary for model-generated and runtime contracts."""

    model_config = ConfigDict(extra="forbid", strict=True)


class DecisionType(StrEnum):
    """Only decisions the single-agent runtime can execute."""

    CALL_TOOLS = "CALL_TOOLS"
    SUBMIT_HYPOTHESIS = "SUBMIT_HYPOTHESIS"
    STOP = "STOP"


class ToolRepeatPolicy(StrEnum):
    """Whether a successful observation can be reused across model turns."""

    FIXED_WINDOW = "FIXED_WINDOW"
    CURRENT_STATE = "CURRENT_STATE"


class StopReason(StrEnum):
    """Controlled reasons for a terminal STOP decision."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVESTIGATION_COMPLETE = "investigation_complete"
    NO_ACTION_NEEDED = "no_action_needed"


class InvestigationErrorCode(StrEnum):
    """Typed semantic failures produced by the deterministic runtime."""

    EMPTY_TOOL_REQUESTS = "EMPTY_TOOL_REQUESTS"
    TOOL_BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"
    DUPLICATE_TOOL_REQUEST = "DUPLICATE_TOOL_REQUEST"
    UNKNOWN_INVESTIGATION_TOOL = "UNKNOWN_INVESTIGATION_TOOL"
    INVALID_TOOL_ARGUMENTS = "INVALID_TOOL_ARGUMENTS"
    EMPTY_EVIDENCE_SET = "EMPTY_EVIDENCE_SET"
    UNKNOWN_HYPOTHESIS_MECHANISM = "UNKNOWN_HYPOTHESIS_MECHANISM"
    INVALID_HYPOTHESIS_SHAPE = "INVALID_HYPOTHESIS_SHAPE"
    INVALID_STOP_REASON = "INVALID_STOP_REASON"
    FABRICATED_EVIDENCE_REFERENCE = "FABRICATED_EVIDENCE_REFERENCE"
    INVALID_DECISION = "INVALID_DECISION"
    DECISION_SCHEMA_INVALID = "DECISION_SCHEMA_INVALID"
    TOOL_ARGUMENT_SCHEMA_INVALID = "TOOL_ARGUMENT_SCHEMA_INVALID"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"


class ValidationStage(StrEnum):
    """Stable boundary at which a deterministic validation failed."""

    PROVIDER_ENVELOPE = "PROVIDER_ENVELOPE"
    PROVIDER_FUNCTION_ARGUMENTS = "PROVIDER_FUNCTION_ARGUMENTS"
    DECISION_SCHEMA = "DECISION_SCHEMA"
    DECISION_SEMANTICS = "DECISION_SEMANTICS"
    TOOL_REGISTRY = "TOOL_REGISTRY"
    TOOL_ARGUMENTS = "TOOL_ARGUMENTS"
    TOOL_BUDGET = "TOOL_BUDGET"
    TOOL_EXECUTION = "TOOL_EXECUTION"
    EVIDENCE_PROVENANCE = "EVIDENCE_PROVENANCE"
    HYPOTHESIS_VALIDATION = "HYPOTHESIS_VALIDATION"
    CONTEXT_BUILD = "CONTEXT_BUILD"
    REQUEST_BUILD = "REQUEST_BUILD"
    PROVIDER_REQUEST_BUILD = "PROVIDER_REQUEST_BUILD"
    PROVIDER_RESPONSE = "PROVIDER_RESPONSE"
    MODEL_DECISION = "MODEL_DECISION"


class HypothesisMechanism(StrEnum):
    """Controlled mechanism vocabulary for the baseline RCA output."""

    SERVICE_ERROR_REGRESSION = "service_error_regression"
    SERVICE_LATENCY_REGRESSION = "service_latency_regression"
    DEPENDENCY_FAILURE = "dependency_failure"
    DEPENDENCY_LATENCY = "dependency_latency"
    DATABASE_CONNECTION_PRESSURE = "database_connection_pressure"
    DATABASE_QUERY_LATENCY = "database_query_latency"
    KAFKA_CONSUMER_LAG = "kafka_consumer_lag"
    CONSUMER_FAILURE = "consumer_failure"
    POD_CRASH = "pod_crash"
    RESOURCE_PRESSURE = "resource_pressure"
    DEPLOYMENT_REGRESSION = "deployment_regression"
    CONFIGURATION_REGRESSION = "configuration_regression"
    UNKNOWN = "unknown"


class ToolRequestSpec(InvestigationModel):
    """One named read-only tool request in a model decision."""

    tool: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


class HypothesisSubmission(InvestigationModel):
    """Model hypothesis that must cite system-generated evidence IDs."""

    affected_component: str = Field(min_length=1, max_length=255)
    mechanism: HypothesisMechanism
    suspected_trigger: str = Field(min_length=1, max_length=1_000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=12)


class InvestigationDecision(InvestigationModel):
    """Validated model output with mutually exclusive decision payloads."""

    decision: DecisionType
    requests: list[ToolRequestSpec] = Field(default_factory=list, max_length=8)
    hypothesis: HypothesisSubmission | None = None
    stop_reason: StopReason | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "InvestigationDecision":
        """Ensure each decision carries exactly the payload it is allowed to use."""
        if self.decision is DecisionType.CALL_TOOLS and not self.requests:
            raise ValueError("CALL_TOOLS requires at least one tool request")
        if self.decision is not DecisionType.CALL_TOOLS and self.requests:
            raise ValueError("only CALL_TOOLS may contain tool requests")
        if self.decision is DecisionType.SUBMIT_HYPOTHESIS and self.hypothesis is None:
            raise ValueError("SUBMIT_HYPOTHESIS requires a hypothesis")
        if self.decision is not DecisionType.SUBMIT_HYPOTHESIS and self.hypothesis is not None:
            raise ValueError("only SUBMIT_HYPOTHESIS may contain a hypothesis")
        if self.decision is DecisionType.STOP and self.stop_reason is None:
            raise ValueError("STOP requires a stop reason")
        if self.decision is not DecisionType.STOP and self.stop_reason is not None:
            raise ValueError("only STOP may contain a stop reason")
        return self


class InvestigationLimits(InvestigationModel):
    """Configurable bounded limits, separate from the v0.2 default policy."""

    HARD_MAX_MODEL_CALLS: ClassVar[int] = 8
    HARD_MAX_TOOL_CALLS: ClassVar[int] = 20
    HARD_MAX_AGENT_TURNS: ClassVar[int] = 8
    HARD_MAX_WALL_TIME_SECONDS: ClassVar[int] = 300

    # These defaults preserve the v0.2 execution policy. A1 may pass a wider
    # explicit configuration after offline performance evaluation.
    max_model_calls: int = Field(default=3, gt=0, le=HARD_MAX_MODEL_CALLS)
    max_tool_calls: int = Field(default=8, gt=0, le=HARD_MAX_TOOL_CALLS)
    max_agent_turns: int = Field(default=3, gt=0, le=HARD_MAX_AGENT_TURNS)
    max_wall_time_seconds: int = Field(default=60, gt=0, le=HARD_MAX_WALL_TIME_SECONDS)


DEFAULT_DEVELOPMENT_LIMITS = {
    "max_model_calls": 3,
    "max_tool_calls": 8,
    "max_agent_turns": 3,
    "max_wall_time_seconds": 60,
}


HARD_RUNTIME_CEILINGS = {
    "max_model_calls": InvestigationLimits.HARD_MAX_MODEL_CALLS,
    "max_tool_calls": InvestigationLimits.HARD_MAX_TOOL_CALLS,
    "max_agent_turns": InvestigationLimits.HARD_MAX_AGENT_TURNS,
    "max_wall_time_seconds": InvestigationLimits.HARD_MAX_WALL_TIME_SECONDS,
}


class A1InvestigationDecision(InvestigationDecision):
    """Wider versioned decision envelope reserved for the A1 experiment."""

    requests: list[ToolRequestSpec] = Field(
        default_factory=list, max_length=InvestigationLimits.HARD_MAX_TOOL_CALLS
    )


class TerminationReason(StrEnum):
    """Stable reason a run stopped producing decisions."""

    HYPOTHESIS_SUBMITTED = "HYPOTHESIS_SUBMITTED"
    AGENT_STOPPED = "AGENT_STOPPED"
    MODEL_CALL_LIMIT = "MODEL_CALL_LIMIT"
    TOOL_CALL_LIMIT = "TOOL_CALL_LIMIT"
    INVALID_DECISION = "INVALID_DECISION"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TOOL_FAILURE = "TOOL_FAILURE"
    WALL_TIME_LIMIT = "WALL_TIME_LIMIT"
    CONTEXT_BUILD_FAILURE = "CONTEXT_BUILD_FAILURE"
    REQUEST_BUILD_FAILURE = "REQUEST_BUILD_FAILURE"
    PROVIDER_REQUEST_BUILD_FAILURE = "PROVIDER_REQUEST_BUILD_FAILURE"


class InvestigationUsage(InvestigationModel):
    """Usage accounting attached to every run."""

    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tool_requests_total: int = Field(ge=0, default=0)
    duplicate_requests_suppressed: int = Field(ge=0, default=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    prompt_hash: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    estimated_api_calls: int = Field(ge=0)
    actual_api_calls: int = Field(ge=0)
    logical_model_turns: int = Field(ge=0, default=0)
    provider_invocations: int = Field(ge=0, default=0)
    outbound_api_attempts: int = Field(ge=0, default=0)
    provider_retries: int = Field(ge=0, default=0)
    shared_ledger_consumed: int = Field(ge=0, default=0)
    model_calls_limit: int = Field(ge=0)
    tool_calls_limit: int = Field(ge=0, default=8)
    terminal_decision: DecisionType | None = None
    stop_reason: StopReason | None = None
    model_budget_exhausted_after_terminal_decision: bool = False
    validation_stage: ValidationStage | None = None
    validation_path: str | None = None
    validator: str | None = None


class InvestigationResult(InvestigationModel):
    """Complete runtime result; evidence is always runtime-owned."""

    run_id: UUID = Field(default_factory=uuid4)
    incident_id: UUID
    hypothesis: HypothesisSubmission | None = None
    causal_hypothesis: dict[str, Any] | None = None
    causal_stop: dict[str, Any] | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=12)
    usage: InvestigationUsage
    termination_reason: TerminationReason
    error_code: str | None = None
    terminal_decision: DecisionType | None = None
    stop_reason: StopReason | None = None
    validation_stage: ValidationStage | None = None
    validation_path: str | None = None
    validator: str | None = None
    turns: list[dict[str, Any]] = Field(
        default_factory=list, max_length=InvestigationLimits.HARD_MAX_AGENT_TURNS
    )
