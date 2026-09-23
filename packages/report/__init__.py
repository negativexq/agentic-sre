"""Canonical, immutable incident report snapshots over a stored diagnosis."""

from packages.report.builder import build_report
from packages.report.model import (
    REPORT_VERSION,
    ReportAgentContribution,
    ReportAgentSafetyAudit,
    ReportAlternative,
    ReportFinding,
    ReportHop,
    ReportInvestigationGap,
    ReportInvestigationState,
    ReportInvestigationSummary,
    ReportInvestigationTurn,
    ReportLifecyclePhase,
    ReportSnapshot,
)
from packages.report.render import to_markdown, to_pdf

__all__ = [
    "REPORT_VERSION",
    "ReportAgentContribution",
    "ReportAgentSafetyAudit",
    "ReportAlternative",
    "ReportFinding",
    "ReportHop",
    "ReportInvestigationGap",
    "ReportInvestigationState",
    "ReportInvestigationSummary",
    "ReportInvestigationTurn",
    "ReportLifecyclePhase",
    "ReportSnapshot",
    "build_report",
    "to_markdown",
    "to_pdf",
]
