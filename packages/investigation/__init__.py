"""Credit-bounded single-agent investigation runtime."""

from packages.investigation.audit import InMemoryInvestigationAuditSink, InvestigationAuditRecord
from packages.investigation.context import CompactContextBuilder
from packages.investigation.contracts import (
    DecisionType,
    InvestigationDecision,
    InvestigationErrorCode,
    InvestigationLimits,
    InvestigationResult,
    StopReason,
    TerminationReason,
    ToolRepeatPolicy,
    ToolRequestSpec,
)
from packages.investigation.duplicates import (
    ToolObservationHistory,
    ToolRequestIdentity,
    make_tool_request_identity,
)
from packages.investigation.prompt import INVESTIGATOR_PROMPT_VERSION, investigator_prompt_hash
from packages.investigation.registry import ReadOnlyToolRegistry, RegisteredTool
from packages.investigation.runtime import InvestigationRuntime

__all__ = [
    "CompactContextBuilder",
    "InMemoryInvestigationAuditSink",
    "DecisionType",
    "InvestigationErrorCode",
    "InvestigationDecision",
    "InvestigationAuditRecord",
    "InvestigationLimits",
    "InvestigationResult",
    "InvestigationRuntime",
    "StopReason",
    "INVESTIGATOR_PROMPT_VERSION",
    "ReadOnlyToolRegistry",
    "RegisteredTool",
    "TerminationReason",
    "ToolRepeatPolicy",
    "ToolObservationHistory",
    "ToolRequestIdentity",
    "ToolRequestSpec",
    "make_tool_request_identity",
    "investigator_prompt_hash",
]
