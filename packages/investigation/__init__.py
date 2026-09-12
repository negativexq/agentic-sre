"""Credit-bounded single-agent investigation runtime."""

from packages.investigation.audit import InMemoryInvestigationAuditSink, InvestigationAuditRecord
from packages.investigation.context import CompactContextBuilder
from packages.investigation.contracts import (
    DecisionType,
    InvestigationDecision,
    InvestigationErrorCode,
    InvestigationLimits,
    InvestigationResult,
    TerminationReason,
    ToolRequestSpec,
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
    "INVESTIGATOR_PROMPT_VERSION",
    "ReadOnlyToolRegistry",
    "RegisteredTool",
    "TerminationReason",
    "ToolRequestSpec",
    "investigator_prompt_hash",
]
